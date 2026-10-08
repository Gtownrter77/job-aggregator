package com.gtownrter77.jobaggregator

import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.net.SocketException
import kotlin.concurrent.thread

/** Runs ConnectionTester against a tiny fake aggregator on localhost (plain JVM, no device). */
class ConnectionTesterTest {
    private lateinit var socket: ServerSocket
    @Volatile private var requiredToken: String? = "abcd-efgh-jkmn-pqrs"
    @Volatile private var healthz = true
    private lateinit var base: String

    @Before
    fun start() {
        socket = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
        base = "http://127.0.0.1:${socket.localPort}"
        thread(isDaemon = true) {
            while (!socket.isClosed) {
                val c = try { socket.accept() } catch (e: SocketException) { break }
                c.use { conn ->
                    val reader = conn.getInputStream().bufferedReader()
                    val path = reader.readLine()?.split(" ")?.getOrNull(1) ?: ""
                    val headers = generateSequence { reader.readLine()?.takeIf { it.isNotEmpty() } }.toList()
                    val token = headers.firstOrNull { it.lowercase().startsWith("x-access-token:") }
                        ?.substringAfter(":")?.trim()
                    val (code, body) = when {
                        path == "/healthz" && healthz ->
                            200 to """{"ok":true,"app":"job-aggregator","token_required":${requiredToken != null}}"""
                        path == "/api/stats" && (requiredToken == null || token == requiredToken) ->
                            200 to """{"total":5040,"by_source":{}}"""
                        path == "/api/stats" -> 401 to """{"error":"access token required"}"""
                        else -> 404 to "not found"
                    }
                    val bytes = body.toByteArray()
                    val out = conn.getOutputStream()
                    out.write(("HTTP/1.1 $code X\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\n" +
                        "Connection: close\r\n\r\n").toByteArray())
                    out.write(bytes)
                    out.flush()
                }
            }
        }
    }

    @After
    fun stop() = socket.close()

    @Test
    fun okWithRightToken() {
        val r = ConnectionTester.test(base, "abcd-efgh-jkmn-pqrs")
        assertTrue(r.message, r.ok)
        assertTrue(r.message, r.message.contains("5040"))
    }

    @Test
    fun missingAndWrongToken() {
        val missing = ConnectionTester.test(base, "")
        assertFalse(missing.ok); assertFalse(missing.unreachable)
        assertTrue(missing.message, missing.message.contains("requires an access token"))
        val wrong = ConnectionTester.test(base, "nope")
        assertFalse(wrong.ok)
        assertTrue(wrong.message, wrong.message.contains("token is wrong"))
    }

    @Test
    fun noTokenServer() {
        requiredToken = null
        assertTrue(ConnectionTester.test(base, "").ok)
    }

    @Test
    fun unreachable() {
        val port = ServerSocket(0).use { it.localPort } // nothing listens here any more
        val r = ConnectionTester.test("http://127.0.0.1:$port", "")
        assertFalse(r.ok)
        assertTrue(r.unreachable)
    }

    @Test
    fun notTheAggregator() {
        healthz = false
        val r = ConnectionTester.test(base, "")
        assertFalse(r.ok)
        assertTrue(r.message, r.message.contains("doesn't look like"))
    }
}
