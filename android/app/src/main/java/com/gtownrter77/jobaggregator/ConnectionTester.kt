package com.gtownrter77.jobaggregator

import java.io.IOException
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.NoRouteToHostException
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/** Blocking connection check; call it off the main thread. */
object ConnectionTester {
    data class Result(val ok: Boolean, val message: String, val unreachable: Boolean = false)

    private const val TIMEOUT_MS = 6000

    fun test(base: String, token: String): Result {
        // 1) /healthz is open even when a token is required: tells "can't reach" apart from "wrong token"
        val health = try {
            get("$base/healthz", null)
        } catch (e: Exception) {
            return Result(false, describe(e, base), unreachable = true)
        }
        if (health.code != 200 || !health.body.contains("\"ok\"")) {
            return Result(
                false,
                "Something answered at $base (HTTP ${health.code}), but it doesn't look like the Job Aggregator. " +
                    "Check the address and port (default 8765)."
            )
        }
        // 2) a protected endpoint, with the token
        val stats = try {
            get("$base/api/stats", token.ifBlank { null })
        } catch (e: Exception) {
            return Result(false, describe(e, base), unreachable = true)
        }
        return when (stats.code) {
            200 -> {
                val total = Regex("\"total\"\\s*:\\s*(\\d+)").find(stats.body)?.groupValues?.get(1)
                Result(true, "Connected to the Job Aggregator" + (total?.let { " — $it jobs in the database." } ?: "."))
            }
            401, 403 -> Result(
                false,
                if (token.isBlank()) "Connected, but this server requires an access token. On the computer run " +
                    "\"python -m aggregator token\" (or open http://localhost:8765/phone there) and type the token above."
                else "Connected, but the access token is wrong. On the computer run \"python -m aggregator token\" " +
                    "to see the current one."
            )
            else -> Result(
                false,
                "The server answered HTTP ${stats.code}. It is running but returned an error; check logs/server.log on the computer."
            )
        }
    }

    private data class Resp(val code: Int, val body: String)

    private fun get(url: String, token: String?): Resp {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = TIMEOUT_MS
            c.readTimeout = TIMEOUT_MS
            c.instanceFollowRedirects = false
            c.setRequestProperty("Accept", "application/json")
            if (token != null) c.setRequestProperty("X-Access-Token", token)
            val code = c.responseCode
            val stream = if (code in 200..399) c.inputStream else c.errorStream
            val body = stream?.bufferedReader()?.use { it.readText().take(20_000) } ?: ""
            return Resp(code, body)
        } finally {
            c.disconnect()
        }
    }

    fun describe(e: Throwable, base: String): String {
        val why = when (e) {
            is UnknownHostException -> "The phone can't find \"${ServerAddress.hostOf(base)}\". Use the computer's IP address instead (e.g. 192.168.1.50)."
            is SocketTimeoutException -> "No answer from $base (timed out). The computer may be off/asleep, on a different network, or its firewall is blocking port ${portOf(base)}."
            is ConnectException -> "Connection refused or failed at $base. The server isn't running in LAN mode, or the firewall is blocking it."
            is NoRouteToHostException -> "No route to $base. The phone and computer are probably on different networks."
            is SSLException -> "Secure connection failed. The aggregator speaks plain http://, not https://."
            is IOException -> "Couldn't connect to $base (${e.message ?: e.javaClass.simpleName})."
            else -> "Couldn't connect to $base (${e.message ?: e.javaClass.simpleName})."
        }
        return why
    }

    private fun portOf(base: String): String =
        Regex(":(\\d+)$").find(base)?.groupValues?.get(1) ?: ServerAddress.DEFAULT_PORT.toString()
}
