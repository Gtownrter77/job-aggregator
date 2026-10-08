package com.gtownrter77.jobaggregator

import android.content.Context

/** Server address + optional access token, saved in SharedPreferences (excluded from backups). */
class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var serverUrl: String?
        get() = sp.getString(KEY_SERVER, null)?.takeIf { it.isNotBlank() }
        set(v) = sp.edit().putString(KEY_SERVER, v).apply()

    var token: String
        get() = sp.getString(KEY_TOKEN, "") ?: ""
        set(v) = sp.edit().putString(KEY_TOKEN, v.trim()).apply()

    companion object {
        private const val KEY_SERVER = "server_url"
        private const val KEY_TOKEN = "access_token"
    }
}
