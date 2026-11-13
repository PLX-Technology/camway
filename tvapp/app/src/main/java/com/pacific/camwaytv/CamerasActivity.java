package com.pacific.camwaytv;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/** Cámaras MANUALES (ver en vivo directo por RTSP, sin servidor). UI nativa:
 *  lista con scroll propio, agregar/editar por diálogo y búsqueda en la red con
 *  selección múltiple. */
public class CamerasActivity extends Activity {

    private ListView list;
    private ArrayAdapter<String> adapter;
    private final List<String> labels = new ArrayList<>();
    private static final int[] SCAN_PORTS = {554, 8554};

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        root.setPadding(pad, dp(16), pad, dp(16));
        setContentView(root);

        // Logo
        ImageView logo = new ImageView(this);
        logo.setImageResource(R.drawable.banner);
        logo.setAdjustViewBounds(true);
        LinearLayout.LayoutParams llp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, dp(64));
        llp.gravity = Gravity.CENTER_HORIZONTAL;
        llp.bottomMargin = dp(8);
        logo.setLayoutParams(llp);
        root.addView(logo);

        TextView title = new TextView(this);
        title.setText("Cámaras manuales");
        title.setTextSize(22);
        title.setGravity(Gravity.CENTER_HORIZONTAL);
        title.setPadding(0, 0, 0, dp(12));
        root.addView(title);

        // Acciones (botones nativos del tema Material)
        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.CENTER_HORIZONTAL);
        Button scan = new Button(this);
        scan.setText("Buscar en la red");
        styleBtn(scan);
        scan.setOnClickListener(v -> scanNetwork());
        Button add = new Button(this);
        add.setText("Agregar cámara");
        styleBtn(add);
        add.setOnClickListener(v -> showEditDialog(-1));
        actions.addView(scan, rowParams());
        actions.addView(add, rowParams());
        root.addView(actions);

        TextView sub = new TextView(this);
        sub.setText("Agregadas (toca para editar o borrar):");
        sub.setPadding(0, dp(12), 0, dp(4));
        root.addView(sub);

        // Lista con scroll propio (ocupa el resto; nada se corta)
        list = new ListView(this);
        adapter = new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, labels);
        list.setAdapter(adapter);
        list.setOnItemClickListener((p, v, pos, id) -> rowMenu(pos));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        list.setLayoutParams(lp);
        root.addView(list);

        refresh();
    }

    private void refresh() {
        labels.clear();
        JSONArray arr = ManualCams.load(this);
        if (arr.length() == 0) {
            labels.add("(todavía no hay cámaras — usa «Buscar en la red» o «Agregar cámara»)");
        } else {
            for (int i = 0; i < arr.length(); i++) {
                JSONObject c = arr.optJSONObject(i);
                if (c != null) labels.add(ManualCams.label(c));
            }
        }
        adapter.notifyDataSetChanged();
    }

    private void rowMenu(int pos) {
        JSONArray arr = ManualCams.load(this);
        if (pos >= arr.length()) return;        // fila placeholder
        new AlertDialog.Builder(this)
                .setItems(new String[]{"Editar", "Borrar"}, (d, which) -> {
                    if (which == 0) showEditDialog(pos);
                    else {
                        ManualCams.removeAt(this, pos);
                        refresh();
                    }
                }).show();
    }

    // ---------------- agregar / editar (diálogo) ----------------
    private void showEditDialog(int pos) {
        JSONObject c = pos >= 0 ? ManualCams.load(this).optJSONObject(pos) : new JSONObject();
        if (c == null) c = new JSONObject();

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int p = dp(20);
        box.setPadding(p, dp(8), p, 0);

        final EditText name = dField(box, "Nombre (ej. Entrada)", c.optString("name"), InputType.TYPE_CLASS_TEXT);
        final EditText ip   = dField(box, "IP (ej. 192.168.1.50)", c.optString("ip"), InputType.TYPE_CLASS_TEXT);
        final EditText port = dField(box, "Puerto", c.optString("port", "554"), InputType.TYPE_CLASS_NUMBER);
        final EditText path = dField(box, "Ruta RTSP (ej. /Streaming/Channels/101)", c.optString("path"), InputType.TYPE_CLASS_TEXT);
        // Para cámaras NUEVAS, prellena con las credenciales predeterminadas.
        final EditText user = dField(box, "Usuario (si la cámara lo pide)",
                pos >= 0 ? c.optString("user") : ManualCams.defUser(this), InputType.TYPE_CLASS_TEXT);
        final EditText pass = dField(box, "Clave (si la cámara lo pide)",
                pos >= 0 ? c.optString("pass") : ManualCams.defPass(this),
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);

        final CheckBox saveDef = new CheckBox(this);
        saveDef.setText("Guardar usuario/clave como predeterminados (para las próximas)");
        saveDef.setChecked(true);
        box.addView(saveDef);

        final Button detect = new Button(this);
        detect.setText("🔎 Detectar ruta automáticamente");
        styleBtn(detect);
        detect.setOnClickListener(v -> {
            String ipv = ip.getText().toString().trim();
            if (ipv.isEmpty()) { toast("Escribe la IP primero"); return; }
            final int pv = parseInt(port.getText().toString().trim(), 554);
            final String u = user.getText().toString().trim();
            final String pw = pass.getText().toString();
            detect.setEnabled(false);
            detect.setText("Buscando ruta…");
            new Thread(() -> {
                final String f = RtspProbe.findPath(ipv, pv, u, pw);
                runOnUiThread(() -> {
                    detect.setEnabled(true);
                    detect.setText("🔎 Detectar ruta automáticamente");
                    if (f != null) { path.setText(f); toast("Ruta detectada: " + f); }
                    else toast("No encontré una ruta. Revisa usuario/clave o escríbela a mano.");
                });
            }).start();
        });
        box.addView(detect);

        TextView tip = new TextView(this);
        tip.setText("Consejo: escribe IP + usuario/clave y pulsa «Detectar ruta». Si no, escríbela: "
                + "Hik /Streaming/Channels/101 · Dahua/IMOU /cam/realmonitor?channel=1&subtype=1 · genéricas /CH001.sdp");
        tip.setTextSize(12);
        tip.setPadding(0, dp(8), 0, 0);
        box.addView(tip);

        android.widget.ScrollView sv = new android.widget.ScrollView(this);
        sv.addView(box);

        final int fpos = pos;
        new AlertDialog.Builder(this)
                .setTitle(pos >= 0 ? "Editar cámara" : "Agregar cámara")
                .setView(sv)
                .setPositiveButton("Guardar", (d, w) -> {
                    String ipv = ip.getText().toString().trim();
                    if (ipv.isEmpty()) { toast("Escribe la IP"); return; }
                    try {
                        JSONObject out = new JSONObject();
                        out.put("name", name.getText().toString().trim());
                        out.put("ip", ipv);
                        String pv = port.getText().toString().trim();
                        out.put("port", pv.isEmpty() ? "554" : pv);
                        out.put("path", path.getText().toString().trim());
                        String uv = user.getText().toString().trim();
                        out.put("user", uv);
                        out.put("pass", pass.getText().toString());
                        if (saveDef.isChecked()) ManualCams.setDefaults(this, uv, pass.getText().toString());
                        JSONArray arr = ManualCams.load(this);
                        if (fpos >= 0) arr.put(fpos, out); else arr.put(out);
                        ManualCams.save(this, arr);
                        refresh();
                    } catch (Exception e) { toast("Error: " + e.getMessage()); }
                })
                .setNegativeButton("Cancelar", null)
                .show();
    }

    // ---------------- escaneo de red (selección múltiple) ----------------
    private void scanNetwork() {
        String base = subnetBase();
        if (base == null) { toast("No pude determinar la red del TV"); return; }
        final AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle("Buscando cámaras…")
                .setMessage("Escaneando " + base + "0/24 (RTSP 554/8554)…")
                .setCancelable(false).create();
        dlg.show();
        new Thread(() -> {
            final List<String> found = scanSubnet(base);
            runOnUiThread(() -> {
                dlg.dismiss();
                if (found.isEmpty()) {
                    new AlertDialog.Builder(this).setTitle("Sin resultados")
                            .setMessage("No encontré cámaras con RTSP abierto en " + base + "0/24.")
                            .setPositiveButton("OK", null).show();
                    return;
                }
                final String[] items = found.toArray(new String[0]);
                final boolean[] checked = new boolean[items.length];
                new AlertDialog.Builder(this)
                        .setTitle(found.size() + " encontrada(s) — marca las que quieras")
                        .setMultiChoiceItems(items, checked, (d, which, isChecked) -> checked[which] = isChecked)
                        .setPositiveButton("Agregar seleccionadas", (d, w) -> {
                            List<String> sel = new ArrayList<>();
                            for (int i = 0; i < items.length; i++) if (checked[i]) sel.add(items[i]);
                            if (sel.isEmpty()) { toast("No marcaste ninguna"); return; }
                            askSharedCredsAndAdd(sel);
                        })
                        .setNegativeButton("Cancelar", null).show();
            });
        }).start();
    }

    /** Tras elegir varias, pide ruta/usuario/clave COMUNES (opcional) y las agrega todas. */
    private void askSharedCredsAndAdd(List<String> sel) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int p = dp(20); box.setPadding(p, dp(8), p, 0);
        TextView t = new TextView(this);
        t.setText("Datos para las " + sel.size() + " cámaras (si comparten usuario/clave/ruta). "
                + "Puedes dejar vacío y completar luego por cámara.");
        t.setTextSize(13); t.setPadding(0, 0, 0, dp(8));
        box.addView(t);
        final EditText path = dField(box, "Ruta RTSP (vacío = detectar sola)", "", InputType.TYPE_CLASS_TEXT);
        final EditText user = dField(box, "Usuario", ManualCams.defUser(this), InputType.TYPE_CLASS_TEXT);
        final EditText pass = dField(box, "Clave", ManualCams.defPass(this),
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        final CheckBox saveDef = new CheckBox(this);
        saveDef.setText("Guardar usuario/clave como predeterminados");
        saveDef.setChecked(true);
        box.addView(saveDef);
        new AlertDialog.Builder(this)
                .setTitle("Agregar " + sel.size() + " cámara(s)")
                .setView(box)
                .setPositiveButton("Agregar", (d, w) -> {
                    final String sharedPath = path.getText().toString().trim();
                    final String u = user.getText().toString().trim();
                    final String pw = pass.getText().toString();
                    if (saveDef.isChecked()) ManualCams.setDefaults(this, u, pw);
                    final AlertDialog prog = new AlertDialog.Builder(this)
                            .setTitle("Agregando…")
                            .setMessage(sharedPath.isEmpty() ? "Detectando la ruta de cada cámara…"
                                                             : "Guardando…")
                            .setCancelable(false).create();
                    prog.show();
                    new Thread(() -> {
                        JSONArray arr = ManualCams.load(this);
                        for (String s : sel) {
                            String ip = s.split(":")[0], port = s.split(":")[1];
                            String pth = sharedPath;
                            if (pth.isEmpty()) {       // auto-detectar la ruta
                                String f = RtspProbe.findPath(ip, parseInt(port, 554), u, pw);
                                if (f != null) pth = f;
                            }
                            try {
                                JSONObject c = new JSONObject();
                                c.put("name", ip); c.put("ip", ip); c.put("port", port);
                                c.put("path", pth); c.put("user", u); c.put("pass", pw);
                                arr.put(c);
                            } catch (Exception ignore) {}
                        }
                        ManualCams.save(this, arr);
                        runOnUiThread(() -> { prog.dismiss(); refresh();
                                toast(sel.size() + " cámara(s) agregada(s)"); });
                    }).start();
                })
                .setNegativeButton("Cancelar", null).show();
    }

    private List<String> scanSubnet(String base) {
        final List<String> out = new CopyOnWriteArrayList<>();
        ExecutorService pool = Executors.newFixedThreadPool(64);
        final CountDownLatch latch = new CountDownLatch(254);
        for (int i = 1; i <= 254; i++) {
            final String host = base + i;
            pool.submit(() -> {
                try {
                    for (int port : SCAN_PORTS) {
                        Socket s = new Socket();
                        try {
                            s.connect(new InetSocketAddress(host, port), 400);
                            out.add(host + ":" + port);
                            break;
                        } catch (IOException ignore) {
                        } finally {
                            try { s.close(); } catch (IOException e) {}
                        }
                    }
                } finally { latch.countDown(); }
            });
        }
        try { latch.await(20, TimeUnit.SECONDS); } catch (InterruptedException ignore) {}
        pool.shutdownNow();
        Collections.sort(out, (a, c) -> {
            try {
                int x = Integer.parseInt(a.substring(a.lastIndexOf('.') + 1, a.indexOf(':')));
                int y = Integer.parseInt(c.substring(c.lastIndexOf('.') + 1, c.indexOf(':')));
                return Integer.compare(x, y);
            } catch (Exception e) { return a.compareTo(c); }
        });
        return out;
    }

    private String subnetBase() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (ni.isLoopback() || !ni.isUp()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a.isLoopbackAddress() || a.getHostAddress().contains(":")) continue;
                    String ip = a.getHostAddress();
                    if (ip.startsWith("169.254.")) continue;
                    int dot = ip.lastIndexOf('.');
                    if (dot > 0) return ip.substring(0, dot + 1);
                }
            }
        } catch (Exception ignore) {}
        return null;
    }

    // ---------------- helpers ----------------
    private int dp(int v) { return (int) (v * getResources().getDisplayMetrics().density); }
    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }
    private static int parseInt(String s, int def) {
        try { return Integer.parseInt(s.trim()); } catch (Exception e) { return def; }
    }
    private void styleBtn(Button b) {
        b.setBackgroundResource(R.drawable.btn_tv);
        b.setTextColor(0xFFFFFFFF);
        b.setAllCaps(false);
        b.setPadding(dp(18), dp(12), dp(18), dp(12));
    }

    private LinearLayout.LayoutParams rowParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(dp(8), 0, dp(8), 0);
        return lp;
    }

    private EditText dField(LinearLayout parent, String hint, String value, int type) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setText(value == null ? "" : value);
        e.setInputType(type);
        e.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        parent.addView(e);
        return e;
    }
}
