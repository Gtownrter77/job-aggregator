package com.gtownrter77.jobaggregator

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

/** Server address + access token, with "Test connection". Shown automatically on first launch. */
class SettingsActivity : AppCompatActivity() {
    private lateinit var prefs: Prefs
    private lateinit var server: EditText
    private lateinit var token: EditText
    private lateinit var result: TextView
    private lateinit var tips: TextView
    private lateinit var testing: ProgressBar
    private lateinit var btnTest: Button
    private lateinit var btnSave: Button
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var firstRun = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        prefs = Prefs(this)
        firstRun = intent.getBooleanExtra(EXTRA_FIRST_RUN, false) || prefs.serverUrl == null
        title = getString(if (firstRun) R.string.welcome_title else R.string.settings_title)
        supportActionBar?.setDisplayHomeAsUpEnabled(!firstRun)

        server = findViewById(R.id.server)
        token = findViewById(R.id.token)
        result = findViewById(R.id.result)
        tips = findViewById(R.id.tips)
        testing = findViewById(R.id.testing)
        btnTest = findViewById(R.id.btn_test)
        btnSave = findViewById(R.id.btn_save)
        findViewById<TextView>(R.id.version).text =
            "Job Aggregator for Android ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}) · ${BuildConfig.APPLICATION_ID}"

        if (savedInstanceState == null) {
            server.setText(prefs.serverUrl ?: "")
            token.setText(prefs.token)
        }
        btnSave.setText(if (firstRun) R.string.save_and_open else R.string.save)
        btnTest.setOnClickListener { runTest(saveAfter = false) }
        btnSave.setOnClickListener { runTest(saveAfter = true) }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (prefs.serverUrl == null) finishAffinity() // nothing to show without a server
                else finish()
            }
        })
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }

    private fun runTest(saveAfter: Boolean) {
        val url = ServerAddress.normalize(server.text.toString())
        if (url == null) {
            server.error = getString(R.string.invalid_address)
            server.requestFocus()
            return
        }
        server.setText(url)
        val tok = token.text.toString().trim()
        setBusy(true)
        result.text = getString(R.string.testing)
        result.setTextColor(ContextCompat.getColor(this, R.color.muted))
        tips.visibility = View.GONE
        io.execute {
            val r = ConnectionTester.test(url, tok)
            main.post {
                if (isFinishing || isDestroyed) return@post
                setBusy(false)
                showResult(url, r)
                if (saveAfter) {
                    if (r.ok) save(url, tok) else confirmSaveAnyway(url, tok, r)
                }
            }
        }
    }

    private fun showResult(url: String, r: ConnectionTester.Result) {
        var msg = r.message
        if (url.startsWith("http://") && !ServerAddress.isPrivateHost(ServerAddress.hostOf(url))) {
            msg += "\n\nNote: this isn't a home-network address, and http:// is not encrypted. " +
                "Only use it over your own Wi-Fi or a VPN (e.g. Tailscale)."
        }
        result.text = msg
        result.setTextColor(ContextCompat.getColor(this, if (r.ok) R.color.ok else R.color.bad))
        tips.visibility = if (r.unreachable) View.VISIBLE else View.GONE
    }

    private fun confirmSaveAnyway(url: String, tok: String, r: ConnectionTester.Result) {
        AlertDialog.Builder(this)
            .setTitle(R.string.not_connected_title)
            .setMessage(r.message + if (r.unreachable) "\n\n" + getString(R.string.tips) else "")
            .setPositiveButton(R.string.save_anyway) { _, _ -> save(url, tok) }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun save(url: String, tok: String) {
        prefs.serverUrl = url
        prefs.token = tok
        finish() // MainActivity reloads with the new settings in onResume
    }

    private fun setBusy(busy: Boolean) {
        testing.visibility = if (busy) View.VISIBLE else View.GONE
        btnTest.isEnabled = !busy
        btnSave.isEnabled = !busy
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_FIRST_RUN = "first_run"
    }
}
