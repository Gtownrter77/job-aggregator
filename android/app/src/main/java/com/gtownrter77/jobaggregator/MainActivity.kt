package com.gtownrter77.jobaggregator

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * One WebView showing the aggregator's web UI from Ryan's computer. Pages of that server
 * stay in the app; every other link (job postings, company sites, mailto:, tel:) opens in
 * the phone's browser / email / phone app.
 */
class MainActivity : AppCompatActivity() {
    private lateinit var prefs: Prefs
    private lateinit var web: WebView
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var progress: ProgressBar
    private lateinit var errorPanel: View
    private lateinit var errorText: TextView

    /** server + token the WebView was last loaded with; reload when settings change */
    private var loadedFor: String? = null
    private var mainFrameError = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = Prefs(this)
        web = findViewById(R.id.web)
        swipe = findViewById(R.id.swipe)
        progress = findViewById(R.id.progress)
        errorPanel = findViewById(R.id.error_panel)
        errorText = findViewById(R.id.error_text)

        with(web.settings) {
            javaScriptEnabled = true          // the UI uses htmx
            domStorageEnabled = true
            builtInZoomControls = true
            displayZoomControls = false
            loadWithOverviewMode = true
            useWideViewPort = true
            setSupportMultipleWindows(false)  // target=_blank -> shouldOverrideUrlLoading
            userAgentString = "$userAgentString JobAggregatorAndroid/${BuildConfig.VERSION_NAME}"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        web.webViewClient = Client()
        web.webChromeClient = object : WebChromeClient() { // also enables the UI's confirm() dialogs
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                progress.progress = newProgress
                progress.visibility = if (newProgress in 1..99) View.VISIBLE else View.GONE
            }
        }

        swipe.setColorSchemeResources(R.color.accent)
        swipe.setOnChildScrollUpCallback { _, _ -> web.scrollY > 0 }
        swipe.setOnRefreshListener {
            if (mainFrameError || web.url == null) loadHome() else web.reload()
        }
        findViewById<Button>(R.id.btn_retry).setOnClickListener { loadHome(keepPath = true) }
        findViewById<Button>(R.id.btn_settings).setOnClickListener { openSettings() }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (!mainFrameError && web.canGoBack()) {
                    web.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState)
            loadedFor = savedInstanceState.getString(KEY_LOADED_FOR)
        }
    }

    override fun onResume() {
        super.onResume()
        val server = prefs.serverUrl
        if (server == null) {
            startActivity(Intent(this, SettingsActivity::class.java).putExtra(SettingsActivity.EXTRA_FIRST_RUN, true))
            return
        }
        if (loadedFor != server + "|" + prefs.token) loadHome()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
        outState.putString(KEY_LOADED_FOR, loadedFor)
    }

    private fun applyTokenCookie(server: String, token: String) {
        val cm = CookieManager.getInstance()
        if (token.isNotBlank()) {
            cm.setCookie(server, "agg_token=$token; Path=/; Max-Age=31536000; SameSite=Lax")
        } else {
            cm.setCookie(server, "agg_token=; Path=/; Max-Age=0")
        }
        cm.flush()
    }

    private fun loadHome(keepPath: Boolean = false, path: String = "/") {
        val server = prefs.serverUrl ?: return openSettings()
        val token = prefs.token
        applyTokenCookie(server, token)
        loadedFor = "$server|$token"
        mainFrameError = false
        errorPanel.visibility = View.GONE
        val current = web.url
        val target = if (keepPath && current != null && ServerAddress.isSameOrigin(server, current)) current
        else server + path
        val headers = if (token.isNotBlank()) mapOf("X-Access-Token" to token) else emptyMap()
        web.loadUrl(target, headers)
    }

    private fun showError(message: String) {
        mainFrameError = true
        errorText.text = message
        errorPanel.visibility = View.VISIBLE
        swipe.isRefreshing = false
        progress.visibility = View.GONE
    }

    private fun openSettings() {
        startActivity(Intent(this, SettingsActivity::class.java))
    }

    private fun openExternally(uri: Uri) {
        val intent = when (uri.scheme?.lowercase()) {
            "mailto" -> Intent(Intent.ACTION_SENDTO, uri)
            "tel" -> Intent(Intent.ACTION_DIAL, uri)
            else -> Intent(Intent.ACTION_VIEW, uri)
        }.addCategory(Intent.CATEGORY_BROWSABLE)
        try {
            startActivity(intent)
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(this, R.string.no_app_for_link, Toast.LENGTH_SHORT).show()
        }
    }

    private inner class Client : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url.toString()
            if (ServerAddress.isSameOrigin(prefs.serverUrl, url)) return false // the aggregator's own pages
            if (url.startsWith("about:") || url.startsWith("javascript:")) return false
            openExternally(request.url)
            return true
        }

        override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
            if (!mainFrameError) errorPanel.visibility = View.GONE
        }

        override fun onPageFinished(view: WebView?, url: String?) {
            swipe.isRefreshing = false
            CookieManager.getInstance().flush()
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            val server = prefs.serverUrl ?: ""
            val reason = when (error.errorCode) {
                ERROR_HOST_LOOKUP -> "The phone can't find \"${ServerAddress.hostOf(server)}\". Use the computer's IP address."
                ERROR_CONNECT -> "Connection refused at $server: the server isn't running in LAN mode, or a firewall blocks it."
                ERROR_TIMEOUT -> "No answer from $server (timed out): computer off/asleep, different network, or firewall."
                else -> "Couldn't load $server (${error.description})."
            }
            showError(reason)
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            R.id.menu_home -> loadHome()
            R.id.menu_followups -> loadHome(path = "/followups")
            R.id.menu_reload -> if (mainFrameError) loadHome(keepPath = true) else web.reload()
            R.id.menu_settings -> openSettings()
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }

    companion object {
        private const val KEY_LOADED_FOR = "loaded_for"
    }
}
