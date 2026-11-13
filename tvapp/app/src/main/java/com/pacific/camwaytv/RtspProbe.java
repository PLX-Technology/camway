package com.pacific.camwaytv;

import android.util.Base64;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.security.MessageDigest;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Detecta automáticamente la RUTA RTSP que funciona en una cámara, probando
 *  rutas comunes con un DESCRIBE (maneja autenticación Digest y Basic). Prefiere
 *  el substream (más liviano para el TV). Sin librerías externas. */
public final class RtspProbe {
    private RtspProbe() {}

    // Orden: substreams primero (livianos), luego principales, luego genéricas.
    public static final String[] PATHS = {
        "/cam/realmonitor?channel=1&subtype=1",   // Dahua / IMOU (sub)
        "/Streaming/Channels/102",                 // Hikvision (sub)
        "/live/ch00_1", "/live/ch01_0",
        "/h264/ch1/sub/av_stream",
        "/CH001.sdp",                              // genéricas HiSilicon (8554)
        "/cam/realmonitor?channel=1&subtype=0",    // Dahua / IMOU (main)
        "/Streaming/Channels/101",                 // Hikvision (main)
        "/h264/ch1/main/av_stream",
        "/11", "/12", "/stream1", "/video1",
        "/live", "/0", "/1", "/"
    };

    /** Devuelve la primera ruta que responde 200, o null. */
    public static String findPath(String ip, int port, String user, String pass) {
        for (String p : PATHS) {
            if (describe(ip, port, p, user, pass)) return p;
        }
        return null;
    }

    private static boolean describe(String ip, int port, String path, String user, String pass) {
        Socket s = new Socket();
        try {
            s.connect(new InetSocketAddress(ip, port), 1500);
            s.setSoTimeout(2500);
            OutputStream out = s.getOutputStream();
            InputStream in = s.getInputStream();
            String url = "rtsp://" + ip + ":" + port + path;
            send(out, "DESCRIBE " + url + " RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n");
            String resp = read(in);
            if (resp.startsWith("RTSP/1.0 200")) return true;
            if (resp.contains(" 401") && user != null && !user.isEmpty()) {
                String auth;
                if (resp.toLowerCase().contains("digest")) {
                    auth = digest(resp, "DESCRIBE", url, user, pass);
                } else {
                    String b = Base64.encodeToString((user + ":" + pass).getBytes(), Base64.NO_WRAP);
                    auth = "Authorization: Basic " + b + "\r\n";
                }
                if (auth == null) return false;
                send(out, "DESCRIBE " + url + " RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n" + auth + "\r\n");
                return read(in).startsWith("RTSP/1.0 200");
            }
            return false;
        } catch (Exception e) {
            return false;
        } finally {
            try { s.close(); } catch (Exception e) {}
        }
    }

    private static void send(OutputStream out, String s) throws Exception {
        out.write(s.getBytes("UTF-8"));
        out.flush();
    }

    private static String read(InputStream in) throws Exception {
        byte[] buf = new byte[4096];
        int n = in.read(buf);
        return n > 0 ? new String(buf, 0, n, "UTF-8") : "";
    }

    private static String val(String src, String key) {
        Matcher m = Pattern.compile(key + "=\"([^\"]*)\"").matcher(src);
        return m.find() ? m.group(1) : "";
    }

    private static String md5(String s) throws Exception {
        MessageDigest d = MessageDigest.getInstance("MD5");
        byte[] h = d.digest(s.getBytes("UTF-8"));
        StringBuilder sb = new StringBuilder();
        for (byte b : h) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static String digest(String resp, String method, String uri, String user, String pass) {
        try {
            String realm = val(resp, "realm");
            String nonce = val(resp, "nonce");
            if (nonce.isEmpty()) return null;
            String ha1 = md5(user + ":" + realm + ":" + pass);
            String ha2 = md5(method + ":" + uri);
            String response = md5(ha1 + ":" + nonce + ":" + ha2);
            return "Authorization: Digest username=\"" + user + "\", realm=\"" + realm
                    + "\", nonce=\"" + nonce + "\", uri=\"" + uri + "\", response=\"" + response + "\"\r\n";
        } catch (Exception e) {
            return null;
        }
    }
}
