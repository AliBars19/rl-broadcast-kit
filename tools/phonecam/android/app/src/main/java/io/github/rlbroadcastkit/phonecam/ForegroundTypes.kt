package io.github.rlbroadcastkit.phonecam

import android.content.pm.ServiceInfo

/** The foreground service type to declare: Android 14+ throws SecurityException for a type whose permission is missing. */
object ForegroundTypes {
    fun pick(hasCamera: Boolean, hasMicrophone: Boolean): Int =
        (if (hasCamera) ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA else 0) or
            (if (hasMicrophone) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
}
