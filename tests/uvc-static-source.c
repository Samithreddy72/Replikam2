/* Execute the shipped STATIC source without any Pi, camera or loopback device. */
#include <time.h>
#include <linux/videodev2.h>
#include <assert.h>
#include <stdlib.h>
#include "video-source.h"
#include "v4l2-source.h"
#include "video-buffers.h"
int main(void)
{
    struct video_source *s = v4l2_video_source_create("/no-such-loopback-device");
    struct v4l2_pix_format fmt = {.width=424, .height=240, .pixelformat=V4L2_PIX_FMT_YUYV};
    struct video_buffer buffer = {.size=424*240*2+4096};
    unsigned int i;
    assert(s && s->type == VIDEO_SOURCE_STATIC);
    assert(s->ops->set_format(s, &fmt) == 0);
    assert(s->ops->stream_on(s) == 0);
    buffer.mem = malloc(buffer.size);
    assert(buffer.mem);
    s->ops->fill_buffer(s, &buffer);
    assert(buffer.bytesused == 424*240*2);
    for (i=0; i<buffer.bytesused; ++i)
        assert(((unsigned char *)buffer.mem)[i] == ((i&1) ? 128 : 16));
    assert(s->ops->stream_off(s) == 0);
    s->ops->destroy(s);
    free(buffer.mem);
    return 0;
}
