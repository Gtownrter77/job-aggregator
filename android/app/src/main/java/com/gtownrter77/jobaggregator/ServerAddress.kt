package com.gtownrter77.jobaggregator

import java.net.URI

/** Pure helpers for the server address (no Android APIs, so they are unit-tested on the JVM). */
object ServerAddress {
    const val DEFAULT_PORT = 8765

    /**
     * Turns what the user typed into "scheme://host:port" (no trailing slash, no path),
     * or null if it isn't usable. "192.168.1.50" -> "http://192.168.1.50:8765".
     */
    fun normalize(input: String?): String? {
        var s = input?.trim() ?: return null
        if (s.isEmpty() || s.any { it.isWhitespace() }) return null
        if (!s.contains("://")) s = "http://$s"
        val uri = try {
            URI(s)
        } catch (e: Exception) {
            return null
        }
        val scheme = uri.scheme?.lowercase() ?: return null
        if (scheme != "http" && scheme != "https") return null
        val host = uri.host ?: return null
        if (host.isEmpty()) return null
        val port = when {
            uri.port > 0 -> uri.port
            // the user typed no port: the aggregator's default for http, none for https (reverse proxy)
            scheme == "http" -> DEFAULT_PORT
            else -> -1
        }
        if (port > 65535) return null
        val hostPart = if (host.contains(':') && !host.startsWith("[")) "[$host]" else host
        return "$scheme://$hostPart" + (if (port > 0) ":$port" else "")
    }

    /** True for addresses that are only reachable on a home/office network or VPN. */
    fun isPrivateHost(hostIn: String?): Boolean {
        val host = hostIn?.trim()?.trim('[', ']')?.lowercase() ?: return false
        if (host.isEmpty()) return false
        if (host == "localhost" || host.endsWith(".local") || host.endsWith(".lan") ||
            host.endsWith(".home") || host.endsWith(".internal") || host.endsWith(".home.arpa") ||
            host.endsWith(".ts.net")
        ) return true
        if (!host.contains('.') && !host.contains(':')) return true // bare machine name, e.g. "ryans-pc"
        val v4 = host.split('.')
        if (v4.size == 4 && v4.all { p -> p.toIntOrNull()?.let { it in 0..255 } == true }) {
            val (a, b) = v4[0].toInt() to v4[1].toInt()
            return a == 10 || a == 127 || (a == 172 && b in 16..31) || (a == 192 && b == 168) ||
                (a == 169 && b == 254) || (a == 100 && b in 64..127) // last: CGNAT / Tailscale
        }
        if (host.contains(':')) {
            return host == "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80")
        }
        return false
    }

    fun hostOf(url: String?): String? {
        if (url == null) return null
        return try {
            URI(url).host
        } catch (e: Exception) {
            null
        }
    }

    /** Same scheme + host + port as the server, i.e. a page of the aggregator itself. */
    fun isSameOrigin(server: String?, url: String?): Boolean {
        if (server == null || url == null) return false
        return try {
            val a = URI(server)
            val b = URI(url)
            val sa = a.scheme?.lowercase()
            val sb = b.scheme?.lowercase()
            sa != null && sa == sb && a.host != null && a.host.equals(b.host, ignoreCase = true) &&
                effectivePort(a) == effectivePort(b)
        } catch (e: Exception) {
            false
        }
    }

    private fun effectivePort(u: URI): Int = when {
        u.port > 0 -> u.port
        u.scheme.equals("https", true) -> 443
        else -> 80
    }
}
