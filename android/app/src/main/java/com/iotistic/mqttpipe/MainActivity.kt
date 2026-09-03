package com.iotistic.mqttpipe

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.Spinner
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

        val mode = findViewById<Spinner>(R.id.mode)
        mode.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item,
            listOf("listen", "connect")
        )
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
            val cfg = JSONObject()
                .put("mode", mode.selectedItem as String)
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

    private fun pollStatus() {
        ui.post(object : Runnable {
            override fun run() {
                val json = Python.getInstance().getModule("app_bridge")
                    .callAttr("status").toString()
                val s = JSONObject(json)
                statusView.text = "status: ${s.optString("state")}  ${s.optString("detail")}"
                ui.postDelayed(this, 1000)
            }
        })
    }
}
