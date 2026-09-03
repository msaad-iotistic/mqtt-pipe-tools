package com.iotistic.mqttpipe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Foreground service that owns the tunnel. Keeps the process alive while
 * backgrounded via: a foreground notification, a partial WakeLock (so the CPU
 * doesn't sleep the paho loop), START_STICKY, and a persisted config so the
 * system can resume the tunnel after killing us.
 */
class TunnelService : Service() {

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val EXTRA_CONFIG = "config"
        private const val CHANNEL_ID = "tunnel"
        private const val NOTIF_ID = 1
        private const val PREFS = "tunnel"
        private const val KEY_CONFIG = "config"
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private fun prefs() = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Null intent = system restarted us (START_STICKY): resume last config.
        val action = intent?.action ?: ACTION_START
        if (action == ACTION_STOP) {
            if (Python.isStarted())
                Python.getInstance().getModule("app_bridge").callAttr("stop")
            prefs().edit().remove(KEY_CONFIG).apply()
            releaseWakeLock()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }

        val cfg = intent?.getStringExtra(EXTRA_CONFIG) ?: prefs().getString(KEY_CONFIG, null)
        if (cfg == null) { stopSelf(); return START_NOT_STICKY }
        prefs().edit().putString(KEY_CONFIG, cfg).apply()   // survive a kill

        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        startForeground(NOTIF_ID, notification())
        acquireWakeLock()
        Python.getInstance().getModule("app_bridge").callAttr("start", cfg)
        return START_STICKY
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "mqttpipe:tunnel")
            .also { it.setReferenceCounted(false); it.acquire() }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    override fun onDestroy() {
        releaseWakeLock()
        super.onDestroy()
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
