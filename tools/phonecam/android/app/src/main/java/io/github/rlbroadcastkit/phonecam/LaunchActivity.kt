package io.github.rlbroadcastkit.phonecam

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * Invisible trampoline. Android only lets a foreground service open the camera when the app was
 * visible at the moment the service started, so `adb shell am start` goes through here.
 */
class LaunchActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val start = Intent(this, CamService::class.java)
            .setAction(CamService.ACTION_START)
            .also { out -> intent.extras?.let { out.putExtras(it) } }
        startForegroundService(start)
        finish()
    }
}
