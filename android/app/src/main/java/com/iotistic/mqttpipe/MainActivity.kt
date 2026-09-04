package com.iotistic.mqttpipe

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.OpenableColumns
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.tabs.TabLayout
import com.google.android.material.textfield.MaterialAutoCompleteTextView
import org.json.JSONObject
import java.io.File

class MainActivity : AppCompatActivity() {

    private val ui = Handler(Looper.getMainLooper())
    private lateinit var statusView: TextView
    private lateinit var whStatus: TextView
    private lateinit var whProgress: LinearProgressIndicator

    private var sendPath: String? = null
    private var sendCodeStr: String = ""
    private var receivedFile: String? = null
    private var awaitingReceive = false

    private val pickFile =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            uri?.let { onPicked(it) }
        }
    private val saveFile =
        registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
            uri?.let { onSaveLocation(it) }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))

        setupTunnelTab()
        setupFilesTab()
        setupTabs()

        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        pollStatus()
        pollWh()
    }

    private fun setupTabs() {
        val tabs = findViewById<TabLayout>(R.id.tabs)
        tabs.addTab(tabs.newTab().setText("Tunnel"))
        tabs.addTab(tabs.newTab().setText("Files"))
        val tunnelPane = findViewById<View>(R.id.tunnelPane)
        val filesPane = findViewById<View>(R.id.filesPane)
        tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab) {
                val files = tab.position == 1
                tunnelPane.visibility = if (files) View.GONE else View.VISIBLE
                filesPane.visibility = if (files) View.VISIBLE else View.GONE
            }
            override fun onTabUnselected(tab: TabLayout.Tab) {}
            override fun onTabReselected(tab: TabLayout.Tab) {}
        })
    }

    private fun setupTunnelTab() {
        val mode = findViewById<MaterialAutoCompleteTextView>(R.id.mode)
        mode.setSimpleItems(arrayOf("listen", "connect"))
        mode.setText("listen", false)
        val address = findViewById<EditText>(R.id.address)
        val broker = findViewById<EditText>(R.id.broker)
        val code = findViewById<EditText>(R.id.code)
        val key = findViewById<EditText>(R.id.key)
        statusView = findViewById(R.id.status)

        findViewById<Button>(R.id.start).setOnClickListener {
            ensureBatteryExemption()
            val cfg = JSONObject()
                .put("mode", mode.text.toString())
                .put("address", address.text.toString())
                .put("broker", broker.text.toString())
                .put("code", code.text.toString())
                .put("key", key.text.toString())
            ContextCompat.startForegroundService(this,
                Intent(this, TunnelService::class.java)
                    .setAction(TunnelService.ACTION_START)
                    .putExtra(TunnelService.EXTRA_CONFIG, cfg.toString()))
        }
        findViewById<Button>(R.id.stop).setOnClickListener {
            startService(Intent(this, TunnelService::class.java).setAction(TunnelService.ACTION_STOP))
        }
    }

    private fun setupFilesTab() {
        whStatus = findViewById(R.id.whStatus)
        whProgress = findViewById(R.id.whProgress)
        findViewById<Button>(R.id.pickFile).setOnClickListener { pickFile.launch(arrayOf("*/*")) }
        findViewById<Button>(R.id.whSend).setOnClickListener { startSend() }
        findViewById<Button>(R.id.whReceive).setOnClickListener { startReceive() }
    }

    private fun onPicked(uri: Uri) {
        val name = queryName(uri) ?: "file.bin"
        val dest = File(cacheDir, name)
        try {
            contentResolver.openInputStream(uri).use { input ->
                dest.outputStream().use { out -> input!!.copyTo(out) }
            }
        } catch (e: Exception) {
            toast("Copy failed: ${e.message}"); return
        }
        sendPath = dest.absolutePath
        sendCodeStr = Python.getInstance().getModule("app_bridge")
            .callAttr("wormhole_new_code").toString()
        findViewById<TextView>(R.id.fileName).text = name
        val codeView = findViewById<TextView>(R.id.sendCode)
        codeView.text = sendCodeStr
        codeView.visibility = View.VISIBLE
        findViewById<Button>(R.id.whSend).isEnabled = true
    }

    private fun startSend() {
        val path = sendPath ?: return
        ensureBatteryExemption()
        val cfg = JSONObject()
            .put("file_path", path)
            .put("code", sendCodeStr)
            .put("broker", findViewById<EditText>(R.id.whBroker).text.toString())
            .put("key", findViewById<EditText>(R.id.whKey).text.toString())
        awaitingReceive = false
        ContextCompat.startForegroundService(this,
            Intent(this, WormholeService::class.java)
                .setAction(WormholeService.ACTION_WH_SEND)
                .putExtra(WormholeService.EXTRA_CONFIG, cfg.toString()))
        whProgress.visibility = View.VISIBLE
    }

    private fun startReceive() {
        val code = findViewById<EditText>(R.id.whRecvCode).text.toString().trim()
        if (code.isEmpty()) { toast("Enter a pairing code"); return }
        ensureBatteryExemption()
        val outDir = File(cacheDir, "received"); outDir.mkdirs()
        val cfg = JSONObject()
            .put("out_dir", outDir.absolutePath)
            .put("code", code)
            .put("broker", findViewById<EditText>(R.id.whBroker).text.toString())
            .put("key", findViewById<EditText>(R.id.whKey).text.toString())
        awaitingReceive = true
        ContextCompat.startForegroundService(this,
            Intent(this, WormholeService::class.java)
                .setAction(WormholeService.ACTION_WH_RECEIVE)
                .putExtra(WormholeService.EXTRA_CONFIG, cfg.toString()))
        whProgress.visibility = View.VISIBLE
    }

    private fun onSaveLocation(uri: Uri) {
        val src = receivedFile ?: return
        try {
            File(src).inputStream().use { input ->
                contentResolver.openOutputStream(uri).use { out -> input.copyTo(out!!) }
            }
            toast("Saved")
        } catch (e: Exception) {
            toast("Save failed: ${e.message}")
        }
    }

    private fun queryName(uri: Uri): String? {
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (i >= 0 && c.moveToFirst()) return c.getString(i)
        }
        return null
    }

    private fun statusColor(state: String): Int = when {
        state.startsWith("run") -> 0xFF2E7D32.toInt()
        state == "done" -> 0xFF2E7D32.toInt()
        state == "error" -> 0xFFC62828.toInt()
        state == "starting" || state == "stopping" -> 0xFFF9A825.toInt()
        else -> 0xFF9E9E9E.toInt()
    }

    private fun pollStatus() {
        ui.post(object : Runnable {
            override fun run() {
                val s = JSONObject(Python.getInstance().getModule("app_bridge")
                    .callAttr("status").toString())
                val state = s.optString("state")
                statusView.text = ("● " + state + "  " + s.optString("detail")).trim()
                statusView.setTextColor(statusColor(state))
                ui.postDelayed(this, 1000)
            }
        })
    }

    private fun pollWh() {
        ui.post(object : Runnable {
            override fun run() {
                val o = JSONObject(Python.getInstance().getModule("app_bridge")
                    .callAttr("wh_status").toString())
                val state = o.optString("state")
                val pct = o.optInt("percent")
                whStatus.text = ("● " + state + "  " + o.optString("detail") +
                        (if (pct in 1..99) "  $pct%" else "")).trim()
                whStatus.setTextColor(statusColor(state))
                when (state) {
                    "running", "starting" -> {
                        whProgress.visibility = View.VISIBLE
                        if (pct > 0) { whProgress.isIndeterminate = false; whProgress.progress = pct }
                        else whProgress.isIndeterminate = true
                    }
                    "done" -> { whProgress.isIndeterminate = false; whProgress.progress = 100 }
                }
                if (awaitingReceive && state == "done") {
                    awaitingReceive = false
                    val f = o.optString("file")
                    if (f.isNotEmpty()) { receivedFile = f; saveFile.launch(File(f).name) }
                }
                ui.postDelayed(this, 700)
            }
        })
    }

    private fun ensureBatteryExemption() {
        if (Build.VERSION.SDK_INT < 23) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        } catch (e: Exception) {}
    }

    private fun toast(m: String) = Toast.makeText(this, m, Toast.LENGTH_SHORT).show()
}
