package com.pacific.camwaytv;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONObject;

/** Cámaras agregadas a mano (IP/puerto/ruta/credenciales) para VER EN VIVO directo
 *  por RTSP, sin pasar por el servidor CamWay. Se guardan en SharedPreferences. */
public final class ManualCams {
    private ManualCams() {}

    public static JSONArray load(Context ctx) {
        SharedPreferences p = ctx.getSharedPreferences("camway", Context.MODE_PRIVATE);
        try {
            return new JSONArray(p.getString("manual_cams", "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    public static void save(Context ctx, JSONArray arr) {
        ctx.getSharedPreferences("camway", Context.MODE_PRIVATE)
           .edit().putString("manual_cams", arr.toString()).apply();
    }

    // Credenciales predeterminadas: se ponen una vez y se prellenan en cada alta.
    public static String defUser(Context ctx) {
        return ctx.getSharedPreferences("camway", Context.MODE_PRIVATE).getString("def_user", "");
    }
    public static String defPass(Context ctx) {
        return ctx.getSharedPreferences("camway", Context.MODE_PRIVATE).getString("def_pass", "");
    }
    public static void setDefaults(Context ctx, String user, String pass) {
        ctx.getSharedPreferences("camway", Context.MODE_PRIVATE)
           .edit().putString("def_user", user).putString("def_pass", pass).apply();
    }

    public static void add(Context ctx, JSONObject cam) {
        JSONArray arr = load(ctx);
        arr.put(cam);
        save(ctx, arr);
    }

    public static void removeAt(Context ctx, int idx) {
        JSONArray arr = load(ctx);
        JSONArray out = new JSONArray();
        for (int i = 0; i < arr.length(); i++) {
            if (i != idx) out.put(arr.optJSONObject(i));
        }
        save(ctx, out);
    }

    /** Construye rtsp://[usuario:clave@]ip:puerto[ruta] con la clave codificada
     *  (para claves con @, : u otros caracteres especiales). */
    public static String buildUrl(JSONObject c) {
        String ip = c.optString("ip", "").trim();
        String port = c.optString("port", "554").trim();
        if (port.isEmpty()) port = "554";
        String path = c.optString("path", "").trim();
        if (!path.isEmpty() && !path.startsWith("/")) path = "/" + path;
        String user = c.optString("user", "").trim();
        String pass = c.optString("pass", "");
        StringBuilder sb = new StringBuilder("rtsp://");
        if (!user.isEmpty()) {
            sb.append(Uri.encode(user));
            if (!pass.isEmpty()) sb.append(":").append(Uri.encode(pass));
            sb.append("@");
        }
        sb.append(ip).append(":").append(port).append(path);
        return sb.toString();
    }

    /** Texto corto para la lista (sin exponer la clave). */
    public static String label(JSONObject c) {
        String name = c.optString("name", "").trim();
        String ip = c.optString("ip", "").trim();
        String port = c.optString("port", "554").trim();
        String head = name.isEmpty() ? ip : name;
        return head + "   (" + ip + ":" + port + ")";
    }
}
