/*
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
/*
 * feeder.c - prove no-underrun continuous live feeding of the glowforge.ko
 * SDMA pulse ring under load.
 *
 * Streams NOP (0x00) pulse bytes to /dev/glowforge, pacing by wall clock to
 * hold a bounded queue depth (like a real grblHAL backend would),
 * with the deadman flock held. The caller locks the motors and the laser
 * latch first: write 1 to /sys/glowforge/cnc/motor_lock and 1 to
 * /sys/glowforge/cnc/laser_latch, and read both back before starting.
 *
 * Reports: feed statistics, worst scheduling stall, ENOMEM count, and the
 * final driver state (expect "running" throughout, "underrun" only after
 * the deliberate starve at the end).
 *
 * Usage: feeder <step_freq_hz> <duration_s> <depth_ms>
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <sched.h>

#define DEV "/dev/glowforge"
#define CNC "/sys/glowforge/cnc/"

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int wr_attr(const char *attr, const char *val)
{
    char path[128];
    int fd, ret;
    snprintf(path, sizeof path, CNC "%s", attr);
    fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    ret = (int)write(fd, val, strlen(val));
    close(fd);
    return ret < 0 ? -1 : 0;
}

static int rd_attr(const char *attr, char *buf, size_t len)
{
    char path[128];
    int fd;
    ssize_t n;
    snprintf(path, sizeof path, CNC "%s", attr);
    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    n = read(fd, buf, len - 1);
    close(fd);
    if (n < 0) return -1;
    while (n > 0 && (buf[n-1] == '\n')) n--;
    buf[n] = 0;
    return 0;
}

int main(int argc, char **argv)
{
    long freq = argc > 1 ? atol(argv[1]) : 10000;
    long duration = argc > 2 ? atol(argv[2]) : 60;
    long depth_ms = argc > 3 ? atol(argv[3]) : 150;
    static unsigned char chunk[8192]; /* NOP bytes: no step, no laser */
    char state[32], u0[16], u1[16];
    long long target, enqueued = 0, enomem = 0, writes = 0;
    double t0, t_end, last = 0, max_stall = 0, max_wr = 0;
    long depth_bytes = (long)((double)freq * depth_ms / 1000.0);
    int fd;

    memset(chunk, 0, sizeof chunk);

    /* SCHED_FIFO like a real feeder; fall back silently if not permitted */
    struct sched_param sp = { .sched_priority = 10 };
    sched_setscheduler(0, SCHED_FIFO, &sp);

    fd = open(DEV, O_WRONLY);
    if (fd < 0) { perror("open " DEV); return 1; }
    if (flock(fd, LOCK_EX) != 0) { perror("flock"); return 1; }

    char fbuf[16];
    snprintf(fbuf, sizeof fbuf, "%ld", freq);
    if (wr_attr("step_freq", fbuf)) { perror("step_freq"); return 1; }
    wr_attr("streaming", "1");
    rd_attr("underruns", u0, sizeof u0);

    lseek(fd, 0, SEEK_SET); /* clear data + position */

    /* Prefill one queue depth, then start the run */
    while (enqueued < depth_bytes) {
        long n = depth_bytes - enqueued;
        if (n > (long)sizeof chunk) n = sizeof chunk;
        if (write(fd, chunk, n) < 0) { perror("prefill"); return 1; }
        enqueued += n;
    }
    if (wr_attr("run", "1")) { perror("run"); return 1; }

    t0 = now_s();
    t_end = t0 + duration;
    last = t0;
    printf("feeding: %ld Hz for %ld s, queue depth %ld ms (%ld bytes)\n",
           freq, duration, depth_ms, depth_bytes);

    while (1) {
        double t = now_s();
        if (t >= t_end) break;
        if (t - last > max_stall) max_stall = t - last;
        last = t;

        /* wall-clock pacing: keep enqueued = consumed-so-far + depth */
        target = (long long)((t - t0) * freq) + depth_bytes;
        while (enqueued < target) {
            long n = (long)(target - enqueued);
            if (n > (long)sizeof chunk) n = sizeof chunk;
            double w0 = now_s(), w1;
            if (write(fd, chunk, n) < 0) {
                if (errno == ENOMEM) { enomem++; break; }
                perror("write"); return 1;
            }
            w1 = now_s();
            if (w1 - w0 > max_wr) max_wr = w1 - w0;
            enqueued += n;
            writes++;
        }

        struct timespec ts = { 0, 20 * 1000 * 1000 }; /* 20 ms */
        nanosleep(&ts, NULL);
    }

    rd_attr("state", state, sizeof state);
    printf("after %ld s: state=%s enqueued=%lld writes=%lld enomem=%lld\n",
           duration, state, enqueued, writes, enomem);
    printf("max loop stall: %.1f ms, max write latency: %.1f ms\n",
           max_stall * 1e3, max_wr * 1e3);
    int fed_ok = (strcmp(state, "running") == 0) && enomem == 0;

    /* Deliberate starve: stop feeding, expect a clean underrun */
    double ts0 = now_s();
    do {
        struct timespec ts = { 0, 50 * 1000 * 1000 };
        nanosleep(&ts, NULL);
        rd_attr("state", state, sizeof state);
    } while (strcmp(state, "running") == 0 && now_s() - ts0 < 10 + depth_ms / 1000.0);
    rd_attr("underruns", u1, sizeof u1);
    printf("after starve: state=%s underruns %s -> %s\n", state, u0, u1);
    int starve_ok = strcmp(state, "underrun") == 0;

    wr_attr("stop", "1");    /* acknowledge */
    lseek(fd, 0, SEEK_SET);
    wr_attr("streaming", "0");
    flock(fd, LOCK_UN);
    close(fd);

    printf("%s: feed %s, starve->underrun %s\n",
           (fed_ok && starve_ok) ? "PASS" : "FAIL",
           fed_ok ? "clean" : "FAILED", starve_ok ? "detected" : "MISSED");
    return (fed_ok && starve_ok) ? 0 : 1;
}
