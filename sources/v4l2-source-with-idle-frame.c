/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Session-bound, decoded-frame fallback; plain YUY2 black on every invalid path. */

#include <linux/videodev2.h>

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#include "bridge-video-frame.h"
#include "events.h"
#include "tools.h"
#include "v4l2.h"
#include "v4l2-source.h"
#include "video-buffers.h"

struct v4l2_source {
	struct video_source src;

	struct v4l2_device *vdev;

	struct nb_frame_cache cache;
};

#define to_v4l2_source(s) container_of(s, struct v4l2_source, src)

static void v4l2_source_destroy(struct video_source *s)
{
	struct v4l2_source *src = to_v4l2_source(s);

	v4l2_close(src->vdev);
	free(src);
}

static int v4l2_source_set_format(struct video_source *s,
				  struct v4l2_pix_format *fmt)
{
	struct v4l2_source *src = to_v4l2_source(s);

	if (fmt->width != 424 || fmt->height != 240 || fmt->pixelformat != V4L2_PIX_FMT_YUYV)
		return -EINVAL;
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
    struct timespec ts;
    /* Drain loopback for compatibility; duplicated loopback buffers never prove freshness. */
    if (v4l2_dequeue_buffer(src->vdev, &sbuf) == 0)
        v4l2_queue_buffer(src->vdev, &sbuf);
    if (buf->size < NB_FRAME_BYTES || clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        nb_black(buf->mem, buf->size);
    } else {
        nb_frame_output(&src->cache, "/run/bridge-pin/video-session",
                        "/run/bridge-video/frame", (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec,
                        buf->mem, NB_FRAME_BYTES);
    }
    buf->bytesused = buf->size < NB_FRAME_BYTES ? buf->size : NB_FRAME_BYTES;
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
