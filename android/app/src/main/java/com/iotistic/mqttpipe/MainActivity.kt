package com.iotistic.mqttpipe

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.net.Uri
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import com.google.android.material.textfield.MaterialAutoCompleteTextView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private val ui = Handler(Looper.getMainLooper())
    private lateinit var statusView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        if (!Python.isStarted()) Python.start(AndroidPlatform(this))

        val mode = findViewById<MaterialAutoCompleteTextView>(R.id.mode)
        mode.setSimpleItems(arrayOf("listen", "connect"))
        mode.setText("listen", false)
        val address = findViewById<EditText>(R.id.address)
        val broker = findViewById<EditText>(R.id.broker)
        val code = findViewById<EditText>(R.id.code)
        val key = findViewById<EditText>(R.id.key)
        statusView = findViewById(R.id.status)

        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        findViewById<Button>(R.id.start).setOnClickListener {
            ensureBatteryExemption()
            val cfg = JSONObject()
                .put("mode", mode.text.toString())
                .put("address", address.text.toString())
                .put("broker", broker.text.toString())
                .put("code", code.text.toString())
                .put("key", key.text.toString())
            val i = Intent(this, TunnelService::class.java)
                .setAction(TunnelService.ACTION_START)
                .putExtra(TunnelService.EXTRA_CONFIG, cfg.toString())
            ContextCompat.startForegroundService(this, i)
        }

        findViewById<Button>(R.id.stop).setOnClickListener {
            startService(
                Intent(this, TunnelService::class.java).setAction(TunnelService.ACTION_STOP)
            )
        }

        pollStatus()
    }

    private fun ensureBatteryExemption() {
        if (Build.VERSION.SDK_INT < 23) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName"))
            )
        } catch (e: Exception) {
            // Some OEM builds don't expose this intent; the FGS + WakeLock still help.
        }
    }

    private fun statusColor(state: String): Int = when {
        state.startsWith("run") -> 0xFF2E7D32.toInt()   // green
        state == "error" -> 0xFFC62828.toInt()          // red
        state == "starting" || state == "stopping" -> 0xFFF9A825.toInt()  // amber
        else -> 0xFF9E9E9E.toInt()                       // grey
    }

    private fun pollStatus() {
        ui.post(object : Runnable {
            override fun run() {
                val json = Python.getInstance().getModule("app_bridge")
                    .callAttr("status").toString()
                val s = JSONObject(json)
                val state = s.optString("state")
                statusView.text = ("● " + state + "  " + s.optString("detail")).trim()
                statusView.setTextColor(statusColor(state))
                ui.postDelayed(this, 1000)
            }
        })
    }
}
