package com.iotistic.mqttpipe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject

/**
 * One-shot foreground service for a wormhole file transfer. Unlike TunnelService
 * it is not sticky (a finished/killed transfer is not resumed). It kicks the
 * bridge send/receive off on its own thread, mirrors progress into a
 * notification, and stops itself when the transfer ends.
 */
class WormholeService : Service() {

    companion object {
        const val ACTION_WH_SEND = "wh_send"
        const val ACTION_WH_RECEIVE = "wh_receive"
        const val ACTION_WH_STOP = "wh_stop"
        const val EXTRA_CONFIG = "config"
        private const val CHANNEL_ID = "wormhole"
        private const val NOTIF_ID = 2
    }

    @Volatile private var monitoring = false

    private fun bridge() = Python.getInstance().getModule("app_bridge")

    override fun onBind(intent: Intent?): IBinder? = null

    private fun teardown() {
        // Stop the poller from re-posting, then explicitly cancel: after
        // stopForeground the notification is a plain one and nm.cancel is what
        // actually removes it (stopForeground alone leaves a re-posted one behind).
        monitoring = false
        getSystemService(NotificationManager::class.java).cancel(NOTIF_ID)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_WH_STOP -> {
                if (Python.isStarted()) bridge().callAttr("wormhole_stop")
                teardown()
                return START_NOT_STICKY
            }
            ACTION_WH_SEND, ACTION_WH_RECEIVE -> {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                startForeground(NOTIF_ID, notification("Starting…", -1))
                val cfg = intent.getStringExtra(EXTRA_CONFIG) ?: "{}"
                val send = intent.action == ACTION_WH_SEND
                bridge().callAttr(if (send) "wormhole_send" else "wormhole_receive", cfg)
                monitoring = true
                Thread {
                    val nm = getSystemService(NotificationManager::class.java)
                    val verb = if (send) "Uploading" else "Downloading"
                    val peer = if (send) "receiver" else "sender"
                    while (true) {
                        val o = JSONObject(bridge().callAttr("wh_status").toString())
                        val st = o.optString("state")
                        val pct = o.optInt("percent")
                        val active = st == "running" || st == "starting"
                        val contacted = o.optBoolean("contacted")
                        val waitingS = o.optInt("waiting_s")
                        val text = when {
                            active && !contacted && waitingS >= 10 -> "No $peer on this code yet"
                            active && !contacted -> "Waiting for $peer…"
                            active -> verb + (if (pct in 1..99) " $pct%" else "…")
                            else -> o.optString("detail").ifEmpty { st }  // sent / disconnected / …
                        }
                        if (!monitoring) break        // a stop happened: don't re-post
                        nm.notify(NOTIF_ID, notification(text, pct))
                        if (st == "done" || st == "error" || st == "idle" || st == "stopped") break
                        Thread.sleep(700)
                    }
                    teardown()
                }.start()
            }
        }
        return START_NOT_STICKY
    }

    private fun notification(text: String, pct: Int): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "File transfer", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val piFlags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        // Tap the notification -> bring the app to the front.
        val openApp = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT),
            piFlags)
        // Stop action -> deliver ACTION_WH_STOP to this (foreground) service.
        val stopPi = PendingIntent.getService(this, 1,
            Intent(this, WormholeService::class.java).setAction(ACTION_WH_STOP), piFlags)
        val b = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_ID) else Notification.Builder(this)
        b.setContentTitle("MQTT Pipe — file transfer")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setContentIntent(openApp)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stopPi)
        if (pct in 0..100) b.setProgress(100, pct, false)
        return b.build()
    }
}
