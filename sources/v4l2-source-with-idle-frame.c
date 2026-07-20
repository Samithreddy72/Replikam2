/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * V4L2 video source — RepliKam INSTRUMENTED build (2026-07-02)
 * STATIC/memcpy pump (v4l2loopback cannot export DMABUF) with:
 *  1. bytesused NEVER left unset: no-frame path emits a full gray frame
 *  2. pump telemetry to stderr every ~2s: DQBUF ok/EAGAIN/err + bytesused
 */

#include <linux/videodev2.h>

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include "events.h"
#include "tools.h"
#include "v4l2.h"
#include "v4l2-source.h"
#include "video-buffers.h"

struct v4l2_source {
	struct video_source src;

	struct v4l2_device *vdev;

	void *last_frame;
	unsigned int last_size;
	unsigned int last_alloc;

	/* The branded "bridge online, waiting for your presenter" card, kept in
	 * its own buffer so live video can never overwrite it. Served whenever
	 * no fresh frame has arrived for IDLE_FALLBACK_S seconds — short network
	 * blips keep the frozen last frame (protects a live meeting), a real
	 * session end decays back to the status card. */
	void *idle_frame;
	unsigned int idle_size;
	/* mtime of the card we currently hold + when we last checked, so a
	 * re-rendered card (Wi-Fi dropped, USB unplugged) reaches the meeting
	 * laptop instead of the boot-time snapshot being shown forever. */
	time_t idle_mtime;
	time_t idle_checked;
	time_t last_fresh;

	unsigned int n_ok, n_again, n_err, n_gray, n_idle;
	int last_errno;
	unsigned int last_used;
	time_t last_report;
};

#define IDLE_FALLBACK_S 5

#define to_v4l2_source(s) container_of(s, struct v4l2_source, src)

static void pump_report(struct v4l2_source *src)
{
	time_t now = time(NULL);
	if (now - src->last_report < 2)
		return;
	src->last_report = now;
	fprintf(stderr, "pump: ok=%u again=%u err=%u(errno=%d) gray=%u idle=%u bytesused=%u\n",
		src->n_ok, src->n_again, src->n_err, src->last_errno,
		src->n_gray, src->n_idle, src->last_used);
	fflush(stderr);
	src->n_ok = src->n_again = src->n_err = src->n_gray = src->n_idle = 0;
}

static void v4l2_source_destroy(struct video_source *s)
{
	struct v4l2_source *src = to_v4l2_source(s);

	free(src->last_frame);
	free(src->idle_frame);
	v4l2_close(src->vdev);
	free(src);
}

static int v4l2_source_set_format(struct video_source *s,
				  struct v4l2_pix_format *fmt)
{
	struct v4l2_source *src = to_v4l2_source(s);

	return v4l2_set_format(src->vdev, fmt);
}

static int v4l2_source_set_frame_rate(struct video_source *s, unsigned int fps)
{
	struct v4l2_source *src = to_v4l2_source(s);

	return v4l2_set_frame_rate(src->vdev, fps);
}

static int v4l2_source_alloc_buffers(struct video_source *s, unsigned int nbufs)
{
	struct v4l2_source *src = to_v4l2_source(s);
	int ret;

	ret = v4l2_alloc_buffers(src->vdev, V4L2_MEMORY_MMAP, nbufs);
	if (ret < 0)
		return ret;

	return v4l2_mmap_buffers(src->vdev);
}

#define IDLE_FRAME_PATH "/etc/bridge/idle-frame.raw"

/* (Re)load the rendered status card. Size comes from the file, so the card
 * follows whatever resolution the renderer produces. Returns 1 on success;
 * a short read (renderer mid-write) leaves the current card untouched. */
static int idle_frame_load(struct v4l2_source *src)
{
	struct stat st;
	FILE *f;
	void *buf;
	size_t got;

	if (stat(IDLE_FRAME_PATH, &st) < 0 || st.st_size <= 0 ||
	    st.st_size > 4 * 1024 * 1024)
		return 0;

	f = fopen(IDLE_FRAME_PATH, "rb");
	if (!f)
		return 0;

	buf = malloc(st.st_size);
	if (!buf) {
		fclose(f);
		return 0;
	}

	got = fread(buf, 1, st.st_size, f);
	fclose(f);

	if (got != (size_t)st.st_size) {
		free(buf);
		return 0;
	}

	free(src->idle_frame);
	src->idle_frame = buf;
	src->idle_size = st.st_size;
	src->idle_mtime = st.st_mtime;
	return 1;
}

/* Once a second while idle, pick up a re-rendered card. Cheap: the renderer
 * only rewrites the file when the status actually changed, so mtime is stable
 * and this is a bare stat() in the common case. */
static void idle_frame_refresh(struct v4l2_source *src)
{
	struct stat st;
	time_t now = time(NULL);

	if (now == src->idle_checked)
		return;
	src->idle_checked = now;

	if (stat(IDLE_FRAME_PATH, &st) == 0 && st.st_mtime != src->idle_mtime)
		idle_frame_load(src);
}

static void v4l2_source_fill_buffer(struct video_source *s, struct video_buffer *buf)
{
	struct v4l2_source *src = to_v4l2_source(s);
	struct video_buffer sbuf;
	unsigned int n;
	int ret;

	ret = v4l2_dequeue_buffer(src->vdev, &sbuf);
	if (ret < 0) {
		if (ret == -EAGAIN)
			src->n_again++;
		else {
			src->n_err++;
			src->last_errno = -ret;
		}
		/* No fresh frame this cycle. If fresh video stopped IDLE_FALLBACK_S+
		 * seconds ago (or never started), serve the status card; otherwise
		 * hold the last real frame so brief blips don't disturb a meeting. */
		idle_frame_refresh(src);
		if (src->idle_frame && src->idle_size &&
		    (src->last_fresh == 0 ||
		     time(NULL) - src->last_fresh >= IDLE_FALLBACK_S)) {
			n = src->idle_size;
			if (n > buf->size)
				n = buf->size;
			memcpy(buf->mem, src->idle_frame, n);
			buf->bytesused = n;
			src->n_idle++;
		} else if (src->last_frame && src->last_size) {
			n = src->last_size;
			if (n > buf->size)
				n = buf->size;
			memcpy(buf->mem, src->last_frame, n);
			buf->bytesused = n;
		} else {
			memset(buf->mem, 0x80, buf->size);
			buf->bytesused = buf->size;
			src->n_gray++;
		}
		src->last_used = buf->bytesused;
		pump_report(src);
		return;
	}

	src->n_ok++;

	n = sbuf.bytesused ? sbuf.bytesused : sbuf.size;
	if (n > buf->size)
		n = buf->size;

	/* v4l2loopback re-serves the last written frame at frame rate forever,
	 * so a successful dequeue does NOT mean fresh video. Only a frame whose
	 * bytes differ from the previous one counts as fresh (a live camera
	 * always differs; a dead source repeats itself exactly). After
	 * IDLE_FALLBACK_S of exact repeats, serve the status card instead. */
	if (src->last_frame && src->last_size == n &&
	    memcmp(src->last_frame, sbuf.mem, n) == 0) {
		if (src->idle_frame && src->idle_size && src->last_fresh &&
		    time(NULL) - src->last_fresh >= IDLE_FALLBACK_S) {
			unsigned int m = src->idle_size;
			if (m > buf->size)
				m = buf->size;
			memcpy(buf->mem, src->idle_frame, m);
			buf->bytesused = m;
			src->last_used = m;
			src->n_idle++;
			v4l2_queue_buffer(src->vdev, &sbuf);
			pump_report(src);
			return;
		}
	} else {
		src->last_fresh = time(NULL);
	}

	memcpy(buf->mem, sbuf.mem, n);
	buf->bytesused = n;
	src->last_used = n;

	if (src->last_alloc < n) {
		free(src->last_frame);
		src->last_frame = malloc(n);
		src->last_alloc = src->last_frame ? n : 0;
	}
	if (src->last_frame) {
		memcpy(src->last_frame, sbuf.mem, n);
		src->last_size = n;
	}

	v4l2_queue_buffer(src->vdev, &sbuf);
	pump_report(src);
}

static int v4l2_source_free_buffers(struct video_source *s)
{
	struct v4l2_source *src = to_v4l2_source(s);

	return v4l2_free_buffers(src->vdev);
}

static int v4l2_source_stream_on(struct video_source *s)
{
	struct v4l2_source *src = to_v4l2_source(s);
	unsigned int i;
	int ret;

	ret = v4l2_source_alloc_buffers(s, 4);
	if (ret < 0) {
		fprintf(stderr, "pump: source alloc FAILED %d\n", ret);
		return ret;
	}

	for (i = 0; i < src->vdev->buffers.nbufs; ++i) {
		struct video_buffer buf = {
			.index = i,
			.size = src->vdev->buffers.buffers[i].size,
			.mem = src->vdev->buffers.buffers[i].mem,
		};

		ret = v4l2_queue_buffer(src->vdev, &buf);
		if (ret < 0) {
			fprintf(stderr, "pump: source QBUF %u FAILED %d\n", i, ret);
			return ret;
		}
	}

	ret = v4l2_stream_on(src->vdev);
	fprintf(stderr, "pump: source STREAMON -> %d (0=ok)\n", ret);
	if (ret < 0)
		return ret;

	return 0;
}

static int v4l2_source_stream_off(struct video_source *s)
{
	struct v4l2_source *src = to_v4l2_source(s);
	int ret;

	ret = v4l2_stream_off(src->vdev);

	v4l2_free_buffers(src->vdev);

	fprintf(stderr, "pump: source STREAMOFF\n");
	return ret;
}

static const struct video_source_ops v4l2_source_ops = {
	.destroy = v4l2_source_destroy,
	.set_format = v4l2_source_set_format,
	.set_frame_rate = v4l2_source_set_frame_rate,
	.alloc_buffers = v4l2_source_alloc_buffers,
	.fill_buffer = v4l2_source_fill_buffer,
	.free_buffers = v4l2_source_free_buffers,
	.stream_on = v4l2_source_stream_on,
	.stream_off = v4l2_source_stream_off,
	.queue_buffer = NULL,
};

struct video_source *v4l2_video_source_create(const char *devname)
{
	struct v4l2_source *src;

	src = malloc(sizeof *src);
	if (!src)
		return NULL;

	memset(src, 0, sizeof *src);
	src->src.ops = &v4l2_source_ops;
	src->src.type = VIDEO_SOURCE_STATIC;

	src->vdev = v4l2_open(devname);
	if (!src->vdev)
		goto err_free_src;

	/* RepliKam: preload the rendered status frame so the host sees
	 * "bridge online, waiting for presenter" until real video arrives
	 * (replaced naturally by the first live frame). */
	if (idle_frame_load(src))
		fprintf(stderr, "pump: idle status frame loaded (%u bytes)\n",
			src->idle_size);

	if (src->vdev->type != V4L2_BUF_TYPE_VIDEO_CAPTURE) {
		fprintf(stderr, "v4l2 device does not support video capture\n");
		goto err_close_v4l2;
	}

	return &src->src;

err_close_v4l2:
	v4l2_close(src->vdev);
err_free_src:
	free(src);

	return NULL;
}

void v4l2_video_source_init(struct video_source *s, struct events *events)
{
	struct v4l2_source *src = to_v4l2_source(s);

	src->src.events = events;
}
