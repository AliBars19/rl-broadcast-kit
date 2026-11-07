package io.github.rlbroadcastkit.phonecam

import org.junit.Assert.assertEquals
import org.junit.Test

class ForegroundTypesTest {
    @Test
    fun theTypeFollowsThePermissionsHeld() {
        assertEquals(0x40 or 0x80, ForegroundTypes.pick(hasCamera = true, hasMicrophone = true))
        assertEquals(0x40, ForegroundTypes.pick(hasCamera = true, hasMicrophone = false))
        assertEquals(0x80, ForegroundTypes.pick(hasCamera = false, hasMicrophone = true))
        assertEquals(0, ForegroundTypes.pick(hasCamera = false, hasMicrophone = false))
    }
}
