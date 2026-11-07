package io.github.rlbroadcastkit.phonecam

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.nio.file.Files

class OrphanRescueTest {
    @Test
    fun partFilesBecomeOrphansAndNothingIsDeleted() {
        val dir = Files.createTempDirectory("orphans").toFile()
        try {
            File(dir, "cam-a-0001-1.mp4.part").writeText("unfinished")
            File(dir, "cam-a-0002-2.mp4").writeText("good")
            File(dir, "cam-a-0003-3.mp4.broken").writeText("broken")
            val logs = mutableListOf<String>()
            assertEquals(1, OrphanRescue.rescue(dir) { logs.add(it) })
            assertEquals(
                listOf("cam-a-0001-1.mp4.orphan", "cam-a-0002-2.mp4", "cam-a-0003-3.mp4.broken"),
                dir.list()!!.sorted(),
            )
            assertEquals("unfinished", File(dir, "cam-a-0001-1.mp4.orphan").readText())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun anExistingOrphanOfTheSameNameIsNotOverwritten() {
        val dir = Files.createTempDirectory("orphans").toFile()
        try {
            File(dir, "cam-a-0001-1.mp4.orphan").writeText("first")
            File(dir, "cam-a-0001-1.mp4.part").writeText("second")
            OrphanRescue.rescue(dir) { }
            assertEquals("first", File(dir, "cam-a-0001-1.mp4.orphan").readText())
            assertEquals("second", File(dir, "cam-a-0001-1.mp4.orphan.1").readText())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun aPartFileThisProcessIsStillClosingIsLeftAlone() {
        val dir = Files.createTempDirectory("orphans").toFile()
        try {
            File(dir, "cam-a-0001-1.mp4.part").writeText("closing")
            File(dir, "cam-a-0002-2.mp4.part").writeText("killed run")
            val logs = mutableListOf<String>()
            assertEquals(1, OrphanRescue.rescue(dir, setOf("cam-a-0001-1.mp4.part")) { logs.add(it) })
            assertEquals(listOf("cam-a-0001-1.mp4.part", "cam-a-0002-2.mp4.orphan"), dir.list()!!.sorted())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun aMissingFolderIsFine() {
        assertEquals(0, OrphanRescue.rescue(File("/nonexistent-phonecam-folder")) { })
    }
}
