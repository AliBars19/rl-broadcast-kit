package io.github.rlbroadcastkit.phonecam

import java.io.DataInputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicLong

/**
 * Serves the live encoded stream on a loopback TCP port (the PC reaches it with adb forward).
 * Every client gets its own bounded queue. A client that falls behind loses its queue and
 * waits for the next keyframe, so a slow reader never slows the encoder or the recording.
 */
class LiveServer(
    private val port: Int,
    private val hello: () -> Hello,
    private val configBytes: () -> ByteArray?,
    private val onKeyFrameWanted: () -> Unit,
    private val onTorch: (Boolean) -> Long,
    private val log: (String) -> Unit,
) {
    val dropped = AtomicLong()
    val clientCount: Int get() = clients.size

    private val clients = CopyOnWriteArrayList<Client>()
    private var server: ServerSocket? = null

    fun start() {
        val socket = ServerSocket(port, 4, InetAddress.getLoopbackAddress())
        server = socket
        Thread({ acceptLoop(socket) }, "live-accept").start()
        log("live server listening on 127.0.0.1:$port")
    }

    fun stop() {
        server?.close()
        clients.forEach { it.close() }
    }

    fun publishConfig(bytes: ByteArray) {
        val packet = Protocol.encodePacket(Protocol.TYPE_CONFIG, 0, 0, Clock.nowUs(), bytes)
        clients.forEach { it.offer(packet, key = true, isConfig = true) }
    }

    fun publishFrame(sample: Sample) {
        if (clients.isEmpty()) return
        val flags = if (sample.key) Protocol.FLAG_KEY else 0
        val packet = Protocol.encodePacket(Protocol.TYPE_FRAME, flags, sample.ptsUs, Clock.nowUs(), sample.bytes)
        clients.forEach { it.offer(packet, sample.key, isConfig = false) }
    }

    private fun acceptLoop(socket: ServerSocket) {
        while (!socket.isClosed) {
            try {
                val accepted = socket.accept()
                accepted.tcpNoDelay = true
                Client(accepted).also { clients.add(it); it.begin() }
            } catch (e: IOException) {
                if (!socket.isClosed) log("accept failed: ${e.message}")
            }
        }
    }

    private inner class Client(private val socket: Socket) {
        private val queue = ArrayBlockingQueue<ByteArray>(QUEUE_PACKETS)
        private val out: OutputStream = socket.getOutputStream()
        @Volatile private var needKey = true
        @Volatile private var closed = false

        fun begin() {
            log("live client connected: ${socket.remoteSocketAddress}")
            queue.offer(Protocol.encodeHello(hello()))
            configBytes()?.let { queue.offer(Protocol.encodePacket(Protocol.TYPE_CONFIG, 0, 0, Clock.nowUs(), it)) }
            onKeyFrameWanted()
            Thread({ writeLoop() }, "live-write").start()
            Thread({ readLoop() }, "live-read").start()
        }

        fun offer(packet: ByteArray, key: Boolean, isConfig: Boolean) {
            if (closed) return
            if (isConfig) { queue.offer(packet); return }
            if (needKey) {
                if (!key) return
                needKey = false
            }
            if (!queue.offer(packet)) {
                queue.clear()
                dropped.incrementAndGet()
                if (key) queue.offer(packet) else needKey = true
            }
        }

        private fun writeLoop() {
            try {
                while (!closed) {
                    val packet = queue.take()
                    synchronized(out) { out.write(packet); out.flush() }
                }
            } catch (e: IOException) {
                log("live client write failed: ${e.message}")
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            } finally {
                close()
            }
        }

        private fun readLoop() {
            val input = DataInputStream(socket.getInputStream())
            val buf = ByteArray(Protocol.COMMAND_SIZE)
            try {
                while (!closed) {
                    input.readFully(buf)
                    handle(Protocol.decodeCommand(buf))
                }
            } catch (e: IOException) {
                if (!closed) log("live client left: ${e.message}")
            } catch (e: IllegalArgumentException) {
                log("live client sent a bad command: ${e.message}")
            } finally {
                close()
            }
        }

        private fun handle(command: Command) {
            when (command.kind) {
                Command.Kind.PING -> sendNow(Protocol.TYPE_PONG, command.arg, ByteArray(0))
                Command.Kind.KEYF -> onKeyFrameWanted()
                Command.Kind.TORCH -> {
                    val at = onTorch(command.arg == 1L)
                    sendNow(Protocol.TYPE_MARK, command.arg, "torch".toByteArray(), at)
                }
            }
        }

        private fun sendNow(type: Int, ptsUs: Long, payload: ByteArray, sendUs: Long = Clock.nowUs()) {
            val packet = Protocol.encodePacket(type, 0, ptsUs, sendUs, payload)
            synchronized(out) { out.write(packet); out.flush() }
        }

        fun close() {
            if (closed) return
            closed = true
            clients.remove(this)
            try { socket.close() } catch (_: IOException) { }
            queue.clear()
            queue.offer(ByteArray(0))
        }
    }

    private companion object {
        const val QUEUE_PACKETS = 90
    }
}
