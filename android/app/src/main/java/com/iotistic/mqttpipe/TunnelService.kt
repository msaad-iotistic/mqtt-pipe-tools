package com.iotistic.mqttpipe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Foreground service that owns the tunnel: keeps the process alive (and the
 * TCP/MQTT sockets open) while the app is backgrounded. The Python engine runs
 * on its own thread inside app_bridge; this service just starts/stops it.
 */
class TunnelService : Service() {

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val EXTRA_CONFIG = "config"
        private const val CHANNEL_ID = "tunnel"
        private const val NOTIF_ID = 1
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                startForeground(NOTIF_ID, notification())
                val cfg = intent.getStringExtra(EXTRA_CONFIG) ?: "{}"
                Python.getInstance().getModule("app_bridge").callAttr("start", cfg)
            }
            ACTION_STOP -> {
                if (Python.isStarted()) {
                    Python.getInstance().getModule("app_bridge").callAttr("stop")
                }
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Tunnel", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val builder = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_ID) else Notification.Builder(this)
        return builder
            .setContentTitle("MQTT Pipe tunnel active")
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .build()
    }
}
