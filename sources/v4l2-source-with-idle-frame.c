/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Session-bound STATIC source. The historical -d selector is retained, but
 * there is deliberately no loopback open/STREAMON: the producer may restart
 * or renegotiate without disturbing USB buffers, descriptors or audio. */
#include <time.h>
#include <linux/videodev2.h>
#include <errno.h>
#include <stdlib.h>
#include "bridge-video-frame.h"
#include "tools.h"
#include "v4l2-source.h"
#include "video-buffers.h"

struct v4l2_source {
    struct video_source src;
    struct nb_frame_cache cache;
};
#define to_v4l2_source(s) container_of(s, struct v4l2_source, src)

static void source_destroy(struct video_source *s)
{
    free(to_v4l2_source(s));
}
static int source_format(struct video_source *s, struct v4l2_pix_format *fmt)
{
    (void)s;
    if (fmt->width != 424 || fmt->height != 240 || fmt->pixelformat != V4L2_PIX_FMT_YUYV)
        return -EINVAL;
    fmt->bytesperline = 424 * 2;
    fmt->sizeimage = NB_FRAME_BYTES;
    return 0;
}
static int source_rate(struct video_source *s, unsigned int fps)
{
    (void)s;
    return fps ? 0 : -EINVAL;
}
static int source_allocate(struct video_source *s, unsigned int count)
{
    (void)s;
    (void)count;
    return 0; /* STATIC mode allocates buffers on the USB sink only. */
}
static int source_noop(struct video_source *s)
{
    (void)s;
    return 0;
}
static void source_fill(struct video_source *s, struct video_buffer *buf)
{
    struct v4l2_source *src = to_v4l2_source(s);
    struct timespec ts;
    if (buf->size < NB_FRAME_BYTES || clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        nb_black(buf->mem, buf->size);
    } else {
        nb_frame_output(&src->cache, "/run/bridge-pin/video-session",
                        "/run/bridge-video/frame", (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec,
                        buf->mem, NB_FRAME_BYTES);
    }
    buf->bytesused = buf->size < NB_FRAME_BYTES ? buf->size : NB_FRAME_BYTES;
}
static const struct video_source_ops source_ops = {
    .destroy = source_destroy,
    .set_format = source_format,
    .set_frame_rate = source_rate,
    .alloc_buffers = source_allocate,
    .fill_buffer = source_fill,
    .free_buffers = source_noop,
    .stream_on = source_noop,
    .stream_off = source_noop,
};
struct video_source *v4l2_video_source_create(const char *devname)
{
    struct v4l2_source *src = calloc(1, sizeof *src);
    (void)devname;
    if (!src)
        return NULL;
    src->src.ops = &source_ops;
    src->src.type = VIDEO_SOURCE_STATIC;
    return &src->src;
}
void v4l2_video_source_init(struct video_source *s, struct events *events)
{
    s->events = events;
}
