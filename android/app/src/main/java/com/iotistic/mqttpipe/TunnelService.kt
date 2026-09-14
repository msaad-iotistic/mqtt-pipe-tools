package com.iotistic.mqttpipe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject

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
    private val poller = Handler(Looper.getMainLooper())
    private fun prefs() = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    override fun onBind(intent: Intent?): IBinder? = null

    // Poll the tunnel status: keep the notification's live text current, and tear
    // the service down when the tunnel ends on its own (server BYE, fatal/auth) so
    // the notification clears instead of implying the tunnel is still up.
    private val watchEnded = object : Runnable {
        override fun run() {
            val o = try {
                JSONObject(Python.getInstance().getModule("app_bridge")
                    .callAttr("status").toString())
            } catch (e: Exception) { JSONObject() }
            val state = o.optString("state")
            val nm = getSystemService(NotificationManager::class.java)
            if (state == "stopped" || state == "error" || state == "done") {
                prefs().edit().remove(KEY_CONFIG).apply()  // don't resurrect a finished tunnel
                releaseWakeLock()
                nm.cancel(NOTIF_ID)                        // plain re-posts need an explicit cancel
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            } else {
                nm.notify(NOTIF_ID, notification(statusText(o)))
                poller.postDelayed(this, 2000)
            }
        }
    }

    private fun statusText(o: JSONObject): String = when {
        o.optString("state") == "running" && o.optString("conn") == "connected" -> "Connected"
        o.optString("state") == "running" && o.optString("conn") == "lost" -> "Reconnecting…"
        o.optString("state") == "running" -> "Connecting…"
        o.optString("state") == "starting" -> "Starting…"
        else -> (o.optString("state") + " " + o.optString("detail")).trim()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Null intent = system restarted us (START_STICKY): resume last config.
        val action = intent?.action ?: ACTION_START
        if (action == ACTION_STOP) {
            poller.removeCallbacks(watchEnded)
            if (Python.isStarted())
                Python.getInstance().getModule("app_bridge").callAttr("stop")
            prefs().edit().remove(KEY_CONFIG).apply()
            releaseWakeLock()
            getSystemService(NotificationManager::class.java).cancel(NOTIF_ID)
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }

        val cfg = intent?.getStringExtra(EXTRA_CONFIG) ?: prefs().getString(KEY_CONFIG, null)
        if (cfg == null) { stopSelf(); return START_NOT_STICKY }
        prefs().edit().putString(KEY_CONFIG, cfg).apply()   // survive a kill

        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        startForeground(NOTIF_ID, notification("Starting…"))
        acquireWakeLock()
        Python.getInstance().getModule("app_bridge").callAttr("start", cfg)
        poller.removeCallbacks(watchEnded)
        poller.postDelayed(watchEnded, 2000)
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
        poller.removeCallbacks(watchEnded)
        releaseWakeLock()
        super.onDestroy()
    }

    private fun notification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Tunnel", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val piFlags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val openApp = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT),
            piFlags)
        val stopPi = PendingIntent.getService(this, 1,
            Intent(this, TunnelService::class.java).setAction(ACTION_STOP), piFlags)
        val builder = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_ID) else Notification.Builder(this)
        return builder
            .setContentTitle("MQTT Pipe tunnel")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setContentIntent(openApp)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stopPi)
            .build()
    }
}
