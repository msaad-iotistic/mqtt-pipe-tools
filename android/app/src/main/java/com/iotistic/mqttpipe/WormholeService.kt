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

    private fun bridge() = Python.getInstance().getModule("app_bridge")

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_WH_STOP -> {
                if (Python.isStarted()) bridge().callAttr("wormhole_stop")
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_WH_SEND, ACTION_WH_RECEIVE -> {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                startForeground(NOTIF_ID, notification("Starting…", -1))
                val cfg = intent.getStringExtra(EXTRA_CONFIG) ?: "{}"
                val send = intent.action == ACTION_WH_SEND
                bridge().callAttr(if (send) "wormhole_send" else "wormhole_receive", cfg)
                Thread {
                    val nm = getSystemService(NotificationManager::class.java)
                    while (true) {
                        val o = JSONObject(bridge().callAttr("wh_status").toString())
                        val st = o.optString("state")
                        val pct = o.optInt("percent")
                        nm.notify(NOTIF_ID, notification("${if (send) "Sending" else "Receiving"}: $st", pct))
                        if (st == "done" || st == "error" || st == "idle") break
                        Thread.sleep(700)
                    }
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf()
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
        val b = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_ID) else Notification.Builder(this)
        b.setContentTitle("MQTT Pipe — file transfer")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
        if (pct in 0..100) b.setProgress(100, pct, false)
        return b.build()
    }
}
