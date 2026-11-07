package io.github.rlbroadcastkit.phonecam

import android.os.SystemClock

/** The one clock everything is stamped with: elapsed realtime (CLOCK_BOOTTIME) in microseconds. */
object Clock {
    fun nowUs(): Long = SystemClock.elapsedRealtimeNanos() / 1000

    /** Wall-clock epoch milliseconds of a time on the elapsed-realtime clock. */
    fun epochMsOf(elapsedUs: Long): Long = System.currentTimeMillis() - (nowUs() - elapsedUs) / 1000
}
