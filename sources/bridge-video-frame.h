/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Room output policy. /run contains only an atomic latest decoded frame, never a queue.
 * The epoch is a non-secret session discriminator, not an authorization credential.
 */
#ifndef BRIDGE_VIDEO_FRAME_H
#define BRIDGE_VIDEO_FRAME_H
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#define NB_FRAME_BYTES (424U * 240U * 2U)
#define NB_HEADER_BYTES 96
#define NB_HOLD_NS UINT64_C(60000000000)
struct nb_frame_cache {
    uint64_t epoch, stamp;
    unsigned char pixels[NB_FRAME_BYTES];
};
static int nb_session(const char *path, uint64_t now, uint64_t *epoch)
{
    uint64_t deadline;
    unsigned int active;
    FILE *f = fopen(path, "r");
    int ok;
    if (!f) return 0;
    ok = fscanf(f, "%" SCNx64 " %u %" SCNu64, epoch, &active, &deadline) == 3;
    fclose(f);
    return ok && active == 1 && *epoch && now < deadline;
}
static void nb_black(unsigned char *out, size_t size)
{
    size_t i;
    for (i = 0; i < size; ++i) out[i] = (i & 1) ? 128 : 16;
}
/* Caller supplies CLOCK_MONOTONIC in ns; independent of wall-clock/NTP. */
static void nb_frame_output(struct nb_frame_cache *cache, const char *control,
                           const char *frame, uint64_t now, unsigned char *out, size_t size)
{
    uint64_t epoch = 0, confirm = 0, incoming = 0, stamp = 0;
    unsigned int bytes = 0;
    char header[NB_HEADER_BYTES + 1];
    FILE *f;
    nb_black(out, size);
    if (size != NB_FRAME_BYTES || !nb_session(control, now, &epoch)) {
        cache->epoch = cache->stamp = 0;
        return;
    }
    if (cache->epoch != epoch) {
        cache->epoch = epoch;
        cache->stamp = 0;
    }
    f = fopen(frame, "rb");
    if (f) {
        if (fread(header, 1, NB_HEADER_BYTES, f) == NB_HEADER_BYTES) {
            header[NB_HEADER_BYTES] = 0;
            if (sscanf(header, "NBV1 %" SCNx64 " %" SCNu64 " %u", &incoming, &stamp, &bytes) == 3 &&
                incoming == epoch && bytes == NB_FRAME_BYTES && stamp > cache->stamp &&
                stamp <= now && now - stamp < NB_HOLD_NS) {
                /* Read into output first: a truncated producer file cannot spoil the cache. */
                if (fread(out, 1, size, f) == size && fgetc(f) == EOF) {
                    memcpy(cache->pixels, out, size);
                    cache->stamp = stamp;
                }
            }
        }
        fclose(f);
    }
    /* A Stop/handover racing a file read takes precedence over the cached picture. */
    if (!nb_session(control, now, &confirm) || confirm != epoch ||
        !cache->stamp || now - cache->stamp >= NB_HOLD_NS) {
        cache->stamp = 0;
        nb_black(out, size);
        return;
    }
    memcpy(out, cache->pixels, size);
}
#endif
