'use strict';

const TARGET_IP = '192.168.1.6';
const TARGET_PORT = 8001;
const MAX_DUMP = 4096;
const AF_INET = 2;
const AF_INET6 = 23;
const SECBUFFER_DATA = 1;
const sockPeers = new Map();
const seenTlsDumps = new Set();

function rememberSeen(key) {
    if (seenTlsDumps.has(key)) {
        return true;
    }
    seenTlsDumps.add(key);
    setTimeout(() => seenTlsDumps.delete(key), 250);
    return false;
}

function findExport(moduleName, exportName) {
    try {
        return Process.getModuleByName(moduleName).findExportByName(exportName);
    } catch (_) {
        return null;
    }
}

function swap16(value) {
    return ((value & 0xff) << 8) | ((value >> 8) & 0xff);
}

function parseSockaddr(addr) {
    if (addr.isNull()) {
        return null;
    }

    const family = addr.readU16();
    if (family === AF_INET) {
        const port = swap16(addr.add(2).readU16());
        const ip = [
            addr.add(4).readU8(),
            addr.add(5).readU8(),
            addr.add(6).readU8(),
            addr.add(7).readU8()
        ].join('.');
        return { family: 'ipv4', ip, port };
    }

    if (family === AF_INET6) {
        const port = swap16(addr.add(2).readU16());
        const parts = [];
        for (let i = 0; i < 8; i++) {
            parts.push(addr.add(8 + (i * 2)).readU16().toString(16));
        }
        return { family: 'ipv6', ip: parts.join(':'), port };
    }

    return { family: 'unknown', ip: 'unknown', port: -1 };
}

function rememberSocket(socket, addr) {
    const peer = parseSockaddr(addr);
    if (peer !== null) {
        sockPeers.set(socket.toString(), peer);
    }
}

function forgetSocket(socket) {
    sockPeers.delete(socket.toString());
}

function getSocketPeer(socket) {
    return sockPeers.get(socket.toString()) || null;
}

function formatPeer(peer) {
    if (peer === null) {
        return 'unknown-peer';
    }
    return peer.ip + ':' + peer.port;
}

function isTargetPeer(peer) {
    if (peer === null) {
        return false;
    }
    if (peer.port !== TARGET_PORT) {
        return false;
    }
    if (TARGET_IP !== null && peer.ip !== TARGET_IP) {
        return false;
    }
    return true;
}

function bytesToHex(ptr, len) {
    const dumpLen = Math.min(len, MAX_DUMP);
    let data;
    try {
        data = new Uint8Array(ptr.readByteArray(dumpLen));
    } catch (_) {
        return null;
    }

    return Array.from(data, b => ('0' + b.toString(16)).slice(-2)).join(' ');
}

function readBytes(ptr, len) {
    try {
        return new Uint8Array(ptr.readByteArray(len));
    } catch (_) {
        return null;
    }
}

function utf8FromBytes(bytes) {
    try {
        return new TextDecoder('utf-8').decode(bytes);
    } catch (_) {
        return null;
    }
}

function isMostlyPrintable(text) {
    if (text === null || text.length === 0) {
        return false;
    }

    let printable = 0;
    for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i);
        if ((c >= 0x20 && c <= 0x7e) || c === 0x0d || c === 0x0a || c === 0x09) {
            printable++;
        }
    }

    return printable / text.length > 0.9;
}

function decodeWebSocket(bytes) {
    if (bytes === null || bytes.length < 2) {
        return null;
    }

    const first = bytes[0];
    const second = bytes[1];
    const fin = (first & 0x80) !== 0;
    const opcode = first & 0x0f;
    const masked = (second & 0x80) !== 0;

    if (!masked) {
        return null;
    }

    if ([0, 1, 2, 8, 9, 10].indexOf(opcode) === -1) {
        return null;
    }

    let payloadLen = second & 0x7f;
    let index = 2;

    if (payloadLen === 126) {
        if (bytes.length < 4) {
            return null;
        }
        payloadLen = (bytes[2] << 8) | bytes[3];
        index = 4;
    } else if (payloadLen === 127) {
        return null;
    }

    let mask = null;
    if (masked) {
        if (bytes.length < index + 4) {
            return null;
        }
        mask = bytes.slice(index, index + 4);
        index += 4;
    }

    if (bytes.length < index + payloadLen) {
        return null;
    }

    const payload = bytes.slice(index, index + payloadLen);
    if (masked && mask !== null) {
        for (let i = 0; i < payload.length; i++) {
            payload[i] ^= mask[i % 4];
        }
    }

    return { fin, opcode, payload };
}

function asciiPreview(ptr, len) {
    const previewLen = Math.min(len, 160);
    if (previewLen <= 0) {
        return null;
    }

    let data;
    try {
        data = new Uint8Array(ptr.readByteArray(previewLen));
    } catch (_) {
        return null;
    }

    let printable = 0;
    let text = '';
    for (const b of data) {
        if (b >= 0x20 && b <= 0x7e) {
            text += String.fromCharCode(b);
            printable++;
        } else if (b === 0x0d) {
            text += '\\r';
        } else if (b === 0x0a) {
            text += '\\n';
        } else if (b === 0x09) {
            text += '\\t';
        } else {
            text += '.';
        }
    }

    return printable > 0 ? text : null;
}

function dumpBuffer(tag, ptr, len, peer) {
    if (ptr.isNull() || len <= 0) {
        return;
    }

    const dumpLen = Math.min(len, MAX_DUMP);
    console.log('\n=== ' + tag + ' peer=' + formatPeer(peer) + ' len=' + len + ' ===');

    const preview = asciiPreview(ptr, len);
    if (preview !== null) {
        console.log('ASCII: ' + preview);
    }

    const hexLine = bytesToHex(ptr, len);
    if (hexLine !== null) {
        console.log('HEX  : ' + hexLine);
    }

    const bytes = readBytes(ptr, Math.min(len, MAX_DUMP));
    const ws = decodeWebSocket(bytes);
    if (ws !== null) {
        const text = utf8FromBytes(ws.payload);
        console.log('WS   : opcode=' + ws.opcode + ' fin=' + ws.fin + ' payloadLen=' + ws.payload.length);
        if (isMostlyPrintable(text)) {
            console.log('WSTXT: ' + text);
        } else {
            console.log('WSHEX: ' + Array.from(ws.payload, b => ('0' + b.toString(16)).slice(-2)).join(' '));
        }
    }

    console.log(hexdump(ptr, {
        offset: 0,
        length: dumpLen,
        header: true,
        ansi: false
    }));

    if (dumpLen < len) {
        console.log('... truncated, total len=' + len);
    }
}

function dumpIfInteresting(tag, socket, ptr, len) {
    const peer = getSocketPeer(socket);
    if (isTargetPeer(peer)) {
        dumpBuffer(tag, ptr, len, peer);
        return;
    }

    if (tag.indexOf('tls') !== -1) {
        dumpBuffer(tag, ptr, len, peer);
    }
}

function hookWinsock() {
    const connectPtr = findExport('ws2_32.dll', 'connect');
    if (connectPtr !== null) {
        Interceptor.attach(connectPtr, {
            onEnter(args) {
                rememberSocket(args[0], args[1]);
                const peer = getSocketPeer(args[0]);
                if (isTargetPeer(peer)) {
                    console.log('\n=== connect target ' + formatPeer(peer) + ' socket=' + args[0] + ' ===');
                }
            }
        });
    }

    const closesocketPtr = findExport('ws2_32.dll', 'closesocket');
    if (closesocketPtr !== null) {
        Interceptor.attach(closesocketPtr, {
            onEnter(args) {
                forgetSocket(args[0]);
            }
        });
    }

    const sendPtr = findExport('ws2_32.dll', 'send');
    if (sendPtr !== null) {
        Interceptor.attach(sendPtr, {
            onEnter(args) {
                dumpIfInteresting('send', args[0], args[1], args[2].toInt32());
            }
        });
    }

    const wsasendPtr = findExport('ws2_32.dll', 'WSASend');
    if (wsasendPtr !== null) {
        const wsabufStride = 4 + 4 + Process.pointerSize;
        const wsabufBufOffset = 8;

        Interceptor.attach(wsasendPtr, {
            onEnter(args) {
                const socket = args[0];
                const buffers = args[1];
                const count = args[2].toInt32();

                for (let i = 0; i < count; i++) {
                    const wsabuf = buffers.add(i * wsabufStride);
                    const len = wsabuf.readU32();
                    const buf = wsabuf.add(wsabufBufOffset).readPointer();
                    dumpIfInteresting('WSASend[' + i + ']', socket, buf, len);
                }
            }
        });
    }
}

function hookEncryptMessage(moduleName) {
    const ptr = findExport(moduleName, 'EncryptMessage');
    if (ptr === null) {
        return false;
    }

    const secBufferStride = 8 + Process.pointerSize;
    Interceptor.attach(ptr, {
        onEnter(args) {
            const desc = args[2];
            if (desc.isNull()) {
                return;
            }

            const count = desc.add(4).readU32();
            const buffers = desc.add(8).readPointer();
            for (let i = 0; i < count; i++) {
                const secBuffer = buffers.add(i * secBufferStride);
                const size = secBuffer.readU32();
                const type = secBuffer.add(4).readU32();
                const data = secBuffer.add(8).readPointer();

                if (type === SECBUFFER_DATA && size > 0 && !data.isNull()) {
                    const hexLine = bytesToHex(data, size);
                    if (hexLine !== null) {
                        const dedupeKey = size + ':' + hexLine;
                        if (rememberSeen(dedupeKey)) {
                            continue;
                        }
                    }
                    dumpBuffer('tls EncryptMessage[' + moduleName + '][' + i + ']', data, size, null);
                }
            }
        }
    });

    return true;
}

function hookDecryptMessage(moduleName) {
    const ptr = findExport(moduleName, 'DecryptMessage');
    if (ptr === null) {
        return false;
    }

    const secBufferStride = 8 + Process.pointerSize;
    Interceptor.attach(ptr, {
        onEnter(args) {
            this.desc = args[1];
        },
        onLeave(retval) {
            if (this.desc === undefined || this.desc.isNull()) {
                return;
            }

            const count = this.desc.add(4).readU32();
            const buffers = this.desc.add(8).readPointer();
            for (let i = 0; i < count; i++) {
                const secBuffer = buffers.add(i * secBufferStride);
                const size = secBuffer.readU32();
                const type = secBuffer.add(4).readU32();
                const data = secBuffer.add(8).readPointer();

                if (type === SECBUFFER_DATA && size > 0 && !data.isNull()) {
                    const hexLine = bytesToHex(data, size);
                    if (hexLine !== null) {
                        const dedupeKey = 'dec:' + size + ':' + hexLine;
                        if (rememberSeen(dedupeKey)) {
                            continue;
                        }
                    }
                    dumpBuffer('tls DecryptMessage[' + moduleName + '][' + i + ']', data, size, null);
                }
            }
        }
    });

    return true;
}

hookWinsock();

const tlsHooks = ['secur32.dll', 'sspicli.dll'].filter(hookEncryptMessage);
['secur32.dll', 'sspicli.dll'].forEach(hookDecryptMessage);
console.log('Frida hooks ready. target=' + TARGET_IP + ':' + TARGET_PORT + ', tls=' + (tlsHooks.length > 0 ? tlsHooks.join(', ') : 'not found'));
