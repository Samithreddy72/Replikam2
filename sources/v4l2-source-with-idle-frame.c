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

	unsigned int n_ok, n_again, n_err, n_gray;
	int last_errno;
	unsigned int last_used;
	time_t last_report;
};

#define to_v4l2_source(s) container_of(s, struct v4l2_source, src)

static void pump_report(struct v4l2_source *src)
{
	time_t now = time(NULL);
	if (now - src->last_report < 2)
		return;
	src->last_report = now;
	fprintf(stderr, "pump: ok=%u again=%u err=%u(errno=%d) gray=%u bytesused=%u\n",
		src->n_ok, src->n_again, src->n_err, src->last_errno,
		src->n_gray, src->last_used);
	fflush(stderr);
	src->n_ok = src->n_again = src->n_err = src->n_gray = 0;
}

static void v4l2_source_destroy(struct video_source *s)
{
	struct v4l2_source *src = to_v4l2_source(s);

	free(src->last_frame);
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
		if (src->last_frame && src->last_size) {
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
	{
		FILE *f = fopen("/etc/bridge/idle-frame.raw", "rb");
		if (f) {
			src->last_frame = malloc(115200);
			if (src->last_frame) {
				src->last_size = fread(src->last_frame, 1, 115200, f);
				src->last_alloc = 115200;
				if (src->last_size != 115200) {
					free(src->last_frame);
					src->last_frame = NULL;
					src->last_size = src->last_alloc = 0;
				} else {
					fprintf(stderr, "pump: idle status frame loaded\n");
				}
			}
			fclose(f);
		}
	}

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
