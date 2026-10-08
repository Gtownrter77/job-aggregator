package com.gtownrter77.jobaggregator

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerAddressTest {
    @Test
    fun normalizeAddsSchemeAndDefaultPort() {
        assertEquals("http://192.168.1.50:8765", ServerAddress.normalize("192.168.1.50"))
        assertEquals("http://192.168.1.50:8765", ServerAddress.normalize("  192.168.1.50:8765/ "))
        assertEquals("http://192.168.1.50:9000", ServerAddress.normalize("http://192.168.1.50:9000/followups"))
        assertEquals("http://ryans-pc.local:8765", ServerAddress.normalize("ryans-pc.local"))
        assertEquals("https://jobs.example.ts.net", ServerAddress.normalize("https://jobs.example.ts.net/"))
        assertEquals("HTTP://10.0.0.2:8765".lowercase(), ServerAddress.normalize("HTTP://10.0.0.2"))
    }

    @Test
    fun normalizeRejectsJunk() {
        assertNull(ServerAddress.normalize(""))
        assertNull(ServerAddress.normalize("   "))
        assertNull(ServerAddress.normalize(null))
        assertNull(ServerAddress.normalize("ftp://192.168.1.5"))
        assertNull(ServerAddress.normalize("192.168.1.5 8765"))
        assertNull(ServerAddress.normalize("http://"))
    }

    @Test
    fun privateHosts() {
        listOf("192.168.1.50", "10.0.0.7", "172.16.4.2", "172.31.255.1", "127.0.0.1", "100.101.102.103",
            "ryans-pc", "ryans-pc.local", "box.lan", "pc.tail1234.ts.net", "localhost", "fd12::1", "[fe80::1]"
        ).forEach { assertTrue(it, ServerAddress.isPrivateHost(it)) }
        listOf("8.8.8.8", "172.32.0.1", "example.com", "100.128.0.1", "", null)
            .forEach { assertFalse(it.toString(), ServerAddress.isPrivateHost(it)) }
    }

    @Test
    fun sameOrigin() {
        val s = "http://192.168.1.50:8765"
        assertTrue(ServerAddress.isSameOrigin(s, "http://192.168.1.50:8765/jobs/abc?x=1"))
        assertTrue(ServerAddress.isSameOrigin(s, "http://192.168.1.50:8765"))
        assertFalse(ServerAddress.isSameOrigin(s, "http://192.168.1.50:8080/"))
        assertFalse(ServerAddress.isSameOrigin(s, "https://192.168.1.50:8765/"))
        assertFalse(ServerAddress.isSameOrigin(s, "https://www.linkedin.com/jobs/view/1"))
        assertFalse(ServerAddress.isSameOrigin(s, "mailto:hr@example.com"))
        assertFalse(ServerAddress.isSameOrigin(null, "http://192.168.1.50:8765/"))
        assertTrue(ServerAddress.isSameOrigin("https://jobs.example.ts.net", "https://jobs.example.ts.net:443/x"))
    }
}
