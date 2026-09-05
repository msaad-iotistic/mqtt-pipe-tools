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
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import android.app.AlertDialog
import android.graphics.Bitmap
import android.text.Editable
import android.text.TextWatcher
import android.widget.ImageView
import com.google.zxing.BarcodeFormat
import com.journeyapps.barcodescanner.BarcodeEncoder
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import java.net.URLDecoder
import java.net.URLEncoder

class MainActivity : AppCompatActivity() {

    private val DEFAULT_LISTEN = "127.0.0.1:2222"
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
    private var scanTarget = "FILES"
    private val scan =
        registerForActivityResult(ScanContract()) { r -> r?.contents?.let { onScan(it) } }

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

        // Sensible default so listen mode is one tap; only prefill when empty.
        if (address.text.isNullOrBlank()) address.setText(DEFAULT_LISTEN)
        mode.setOnItemClickListener { _, _, _, _ ->
            if (mode.text.toString() == "listen" && address.text.isNullOrBlank())
                address.setText(DEFAULT_LISTEN)
        }

        findViewById<Button>(R.id.start).setOnClickListener {
            ensureBatteryExemption()
            val cfg = JSONObject()
                .put("mode", mode.text.toString())
                .put("address", address.text.toString())
                .put("broker", broker.text.toString())
                .put("code", code.text.toString())
                .put("key", key.text.toString())
            saveHistory(cfg)
            ContextCompat.startForegroundService(this,
                Intent(this, TunnelService::class.java)
                    .setAction(TunnelService.ACTION_START)
                    .putExtra(TunnelService.EXTRA_CONFIG, cfg.toString()))
        }
        findViewById<Button>(R.id.stop).setOnClickListener {
            startService(Intent(this, TunnelService::class.java).setAction(TunnelService.ACTION_STOP))
        }
        findViewById<Button>(R.id.tunFromCmd).setOnClickListener { showFromCommandDialog() }
        findViewById<Button>(R.id.tunRecent).setOnClickListener { showHistoryDialog() }
        findViewById<Button>(R.id.tunShowQr).setOnClickListener {
            showQrDialog(buildPayload(code.text.toString(), broker.text.toString(), key.text.toString()))
        }
        findViewById<Button>(R.id.tunScan).setOnClickListener { scanTarget = "TUNNEL"; launchScan() }
    }

    private fun applyTunnelConfig(o: JSONObject) {
        o.optString("mode").takeIf { it.isNotEmpty() }?.let {
            findViewById<MaterialAutoCompleteTextView>(R.id.mode).setText(it, false)
        }
        fun set(id: Int, k: String) { if (o.has(k)) findViewById<EditText>(id).setText(o.optString(k)) }
        set(R.id.address, "address"); set(R.id.broker, "broker")
        set(R.id.code, "code"); set(R.id.key, "key")
    }

    private fun showFromCommandDialog() {
        val et = EditText(this)
        et.hint = "mqtt-forward --connect host:22 --broker emqx --code …"
        et.setPadding(48, 32, 48, 32)
        AlertDialog.Builder(this).setTitle("Start from command").setView(et)
            .setPositiveButton("Fill") { _, _ ->
                val out = Python.getInstance().getModule("app_bridge")
                    .callAttr("parse_command", et.text.toString()).toString()
                val o = JSONObject(out)
                if (o.has("error")) toast(o.getString("error"))
                else { applyTunnelConfig(o); toast("Filled from command") }
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun histPrefs() = getSharedPreferences("history", Context.MODE_PRIVATE)

    private fun saveHistory(cfg: JSONObject) {
        // Most-recent-first, drop an identical earlier entry, cap at 10.
        val prev = JSONArray(histPrefs().getString("tunnel", "[]"))
        val out = JSONArray().put(cfg)
        val seen = cfg.toString()
        var i = 0
        while (i < prev.length() && out.length() < 10) {
            val o = prev.getJSONObject(i); i++
            if (o.toString() != seen) out.put(o)
        }
        histPrefs().edit().putString("tunnel", out.toString()).apply()
    }

    private fun showHistoryDialog() {
        val arr = JSONArray(histPrefs().getString("tunnel", "[]"))
        if (arr.length() == 0) { toast("No saved sessions yet"); return }
        val labels = Array(arr.length()) { i ->
            val o = arr.getJSONObject(i)
            val c = o.optString("code")
            "${o.optString("mode")} ${o.optString("address")} · ${o.optString("broker")}" +
                    (if (c.isNotEmpty()) " · $c" else "")
        }
        AlertDialog.Builder(this).setTitle("Recent sessions")
            .setItems(labels) { _, i -> applyTunnelConfig(arr.getJSONObject(i)); toast("Loaded") }
            .setNegativeButton("Close", null).show()
    }

    private fun setupFilesTab() {
        whStatus = findViewById(R.id.whStatus)
        whProgress = findViewById(R.id.whProgress)
        findViewById<Button>(R.id.pickFile).setOnClickListener { pickFile.launch(arrayOf("*/*")) }
        findViewById<Button>(R.id.whSend).setOnClickListener { startSend() }
        findViewById<Button>(R.id.whReceive).setOnClickListener { startReceive() }
        findViewById<Button>(R.id.whScan).setOnClickListener { scanTarget = "FILES"; launchScan() }
        val refresh = object : TextWatcher {
            override fun afterTextChanged(s: Editable?) { refreshSendQr() }
            override fun beforeTextChanged(a: CharSequence?, b: Int, c: Int, d: Int) {}
            override fun onTextChanged(a: CharSequence?, b: Int, c: Int, d: Int) {}
        }
        findViewById<EditText>(R.id.whKey).addTextChangedListener(refresh)
        findViewById<EditText>(R.id.whBroker).addTextChangedListener(refresh)
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
        refreshSendQr()
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

    private fun launchScan() {
        scan.launch(ScanOptions()
            .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            .setBeepEnabled(false).setOrientationLocked(false)
            .setPrompt("Scan pairing QR"))
    }

    private fun qrBitmap(text: String): Bitmap =
        BarcodeEncoder().encodeBitmap(text, BarcodeFormat.QR_CODE, 512, 512)

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    private fun buildPayload(code: String, broker: String, key: String): String {
        val sb = StringBuilder("mqttpipe:code=").append(enc(code))
        if (broker.isNotEmpty()) sb.append("&broker=").append(enc(broker))
        if (key.isNotEmpty()) sb.append("&key=").append(enc(key))
        return sb.toString()
    }

    private fun onScan(text: String) {
        val m = HashMap<String, String>()
        if (text.startsWith("mqttpipe:")) {
            for (kv in text.removePrefix("mqttpipe:").split("&")) {
                val i = kv.indexOf('=')
                if (i > 0) m[kv.substring(0, i)] = URLDecoder.decode(kv.substring(i + 1), "UTF-8")
            }
        } else m["code"] = text.trim()
        val code = m["code"] ?: ""; val broker = m["broker"] ?: ""; val key = m["key"] ?: ""
        if (scanTarget == "TUNNEL") {
            if (code.isNotEmpty()) findViewById<EditText>(R.id.code).setText(code)
            if (broker.isNotEmpty()) findViewById<EditText>(R.id.broker).setText(broker)
            if (key.isNotEmpty()) findViewById<EditText>(R.id.key).setText(key)
        } else {
            if (code.isNotEmpty()) findViewById<EditText>(R.id.whRecvCode).setText(code)
            if (broker.isNotEmpty()) findViewById<EditText>(R.id.whBroker).setText(broker)
            if (key.isNotEmpty()) findViewById<EditText>(R.id.whKey).setText(key)
        }
        toast("Scanned code: $code")
    }

    private fun refreshSendQr() {
        val iv = findViewById<ImageView>(R.id.whSendQr)
        if (sendCodeStr.isEmpty()) { iv.visibility = View.GONE; return }
        try {
            iv.setImageBitmap(qrBitmap(buildPayload(
                sendCodeStr,
                findViewById<EditText>(R.id.whBroker).text.toString(),
                findViewById<EditText>(R.id.whKey).text.toString())))
            iv.visibility = View.VISIBLE
        } catch (e: Exception) { iv.visibility = View.GONE }
    }

    private fun showQrDialog(payload: String) {
        val iv = ImageView(this)
        iv.setImageBitmap(qrBitmap(payload))
        iv.setPadding(32, 32, 32, 32)
        AlertDialog.Builder(this).setTitle("Scan to pair").setView(iv)
            .setPositiveButton("Close", null).show()
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
