//! `oura-phone-core` — noyau FFI pour l'app Android Oura.
//!
//! Réutilise les crates vendues `oura-protocol` / `oura-link` / `oura-store`
//! (pinned : Th0rgal/open_oura @ 945470c).
//!
//! Contrat avec Kotlin :
//!   * Kotlin connecte le GATT (adresse vue au scan) et s'abonne aux
//!     notifications sur la caractéristique notify Oura.
//!   * Kotlin appelle `core_start_sync()` une fois la connexion établie.
//!   * Kotlin pompe `core_next_write()` → écrit chaque paquet sur la
//!     caractéristique write (WriteType WITH_RESPONSE), séquentiellement.
//!   * Chaque notification reçue → `core_feed(bytes)`.
//!   * `core_status()` renvoie l'état JSON (state / detail / curseur / erreur).
//!   * En `done`, `core_recent_events(limit)` fournit le delta à pousser
//!     vers `serv` (le serveur dédup sur UNIQUE(serial, tag, ring_timestamp, body)).
//!
//! Toutes les fonctions sont thread-safe ; le flux de sync tourne sur un
//! runtime tokio interne (current-thread).

use std::collections::VecDeque;
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::sync::{Arc, Mutex};

use anyhow::Context as _;
use async_trait::async_trait;
use oura_link::client::OuraClient;
use oura_link::error::Result as LinkResult;
use oura_link::transport::Transport;
use oura_protocol::auth::AuthResult;
use oura_store::Store;

// ---------------------------------------------------------------------------
// Transport pont vers Kotlin (file d'écriture + canal de notifications)
// ---------------------------------------------------------------------------

struct AndroidTransport {
    out: Arc<Mutex<VecDeque<Vec<u8>>>>,
    in_tx: tokio::sync::broadcast::Sender<Vec<u8>>,
}

#[async_trait]
impl Transport for AndroidTransport {
    async fn write(&self, data: &[u8]) -> LinkResult<()> {
        self.out.lock().unwrap().push_back(data.to_vec());
        Ok(())
    }
    fn subscribe(&self) -> tokio::sync::broadcast::Receiver<Vec<u8>> {
        self.in_tx.subscribe()
    }
}

// ---------------------------------------------------------------------------
// État partagé
// ---------------------------------------------------------------------------

#[derive(Default)]
struct Status {
    state: String, // idle | auth | setup | drain | done | error
    detail: String,
    serial: String,
    auth: String,
    events: i64,
    inserted: i64,
    cursor: i64,
    bytes_left: i64,
    battery: i64,
}

impl Status {
    fn json(&self) -> String {
        serde_json::json!({
            "state": self.state,
            "detail": self.detail,
            "serial": self.serial,
            "auth": self.auth,
            "events": self.events,
            "inserted": self.inserted,
            "cursor": self.cursor,
            "bytes_left": self.bytes_left,
            "battery": self.battery,
        })
        .to_string()
    }
}

struct Inner {
    out: Arc<Mutex<VecDeque<Vec<u8>>>>,
    in_tx: tokio::sync::broadcast::Sender<Vec<u8>>,
    store: Arc<Mutex<Store>>,
    status: Arc<Mutex<Status>>,
    key: [u8; 16],
}

pub struct Core {
    runtime: tokio::runtime::Runtime,
    inner: Arc<Inner>,
}

// ---------------------------------------------------------------------------
// Flux de sync (miroir de `cmd_sync` du CLI PC)
// ---------------------------------------------------------------------------

async fn run_sync(inner: Arc<Inner>) {
    let status = inner.status.clone();
    let set = |st: &str, detail: &str| {
        let mut s = status.lock().unwrap();
        s.state = st.into();
        s.detail = detail.into();
    };

    let transport = AndroidTransport {
        out: inner.out.clone(),
        in_tx: inner.in_tx.clone(),
    };
    let client = OuraClient::new(transport);
    let key = inner.key;
    let store = inner.store.clone();

    // auth applicative
    set("auth", "authentification app-level…");
    let auth = match client.authenticate(&key).await {
        Ok(a) => a,
        Err(e) => {
            set("error", &format!("auth: {e}"));
            return;
        }
    };
    let auth_name = match auth {
        AuthResult::Success => "success",
        AuthResult::AuthenticationError => "authentication_error",
        AuthResult::InFactoryReset => "in_factory_reset",
        AuthResult::NotOriginalOnboardedDevice => "not_original_onboarded_device",
        AuthResult::Unknown(c) => {
            set("error", &format!("auth inconnu: 0x{c:02x}"));
            return;
        }
    };
    status.lock().unwrap().auth = auth_name.into();
    if !auth.is_success() {
        set("error", &format!("auth refusée : {auth_name}"));
        return;
    }

    // setup app-style + time-sync
    set("setup", "stream setup + time-sync…");
    if let Err(e) = client.setup_app_stream().await {
        set("error", &format!("setup: {e}"));
        return;
    }
    if let Err(e) = client.sync_time_app().await {
        set("error", &format!("sync_time: {e}"));
        return;
    }

    // identité + batterie
    let serial = client.serial().await.unwrap_or_else(|_| "unknown".into());
    let info = client.firmware().await.ok();
    let battery = client.battery().await.ok();
    {
        let mut s = status.lock().unwrap();
        s.serial = serial.clone();
        if let Some(b) = &battery {
            s.battery = b.percent as i64;
        }
    }

    // store : device + curseur
    {
        let st = store.lock().unwrap();
        if let Err(e) = st.upsert_device(&serial, None, info.as_ref()) {
            set("error", &format!("device upsert: {e}"));
            return;
        }
        match st.cursor(&serial) {
            Ok(c) => status.lock().unwrap().cursor = c as i64,
            Err(e) => {
                set("error", &format!("curseur: {e}"));
                return;
            }
        }
    }
    let cursor = store.lock().unwrap().cursor(&serial).unwrap_or(0);

    // drain
    set("drain", "drain des événements…");
    let pending: Mutex<Vec<oura_protocol::events::RingEvent>> = Mutex::new(Vec::new());
    let outcome = match client
        .drain_events(
            cursor,
            |ev| {
                pending.lock().unwrap().push(ev.clone());
                true
            },
            |p| {
                let events = std::mem::take(&mut *pending.lock().unwrap());
                let st = store.lock().unwrap();
                match st.commit_batch(&serial, &events, p.next_cursor) {
                    Ok(n) => {
                        let mut s = status.lock().unwrap();
                        s.cursor = p.next_cursor as i64;
                        s.bytes_left = p.bytes_left as i64;
                        s.events = p.events_synced as i64;
                        s.inserted += n as i64;
                    }
                    Err(e) => {
                        set("error", &format!("db: {e}"));
                        return false;
                    }
                }
                true
            },
        )
        .await
    {
        Ok(o) => o,
        Err(e) => {
            set("error", &format!("drain: {e}"));
            return;
        }
    };
    {
        if let Err(e) = store.lock().unwrap().set_cursor(&serial, outcome.next_cursor) {
            set("error", &format!("curseur final: {e}"));
            return;
        }
    }
    let s = status.lock().unwrap();
    let detail = format!(
        "{} événements reçus, {} insérés, curseur {}",
        outcome.events_synced, s.inserted, outcome.next_cursor
    );
    drop(s);
    set("done", &detail);
}

/// Cérémonie de bascule (miroir de `bootstrap-appairage.sh` côté PC) :
///   anneau factory-reset (keyless) → set_auth_key (notre clé) → authenticate
///   → setup + time-sync → features HR/SpO2 → sleep-analyze → drain complet.
/// Le bond SMP est fait côté Kotlin (createBond) avant d'appeler ce flux.
async fn run_ceremony(inner: Arc<Inner>) {
    use oura_protocol::protocol::{feature, feature_mode};

    let status = inner.status.clone();
    let set = |st: &str, detail: &str| {
        let mut s = status.lock().unwrap();
        s.state = st.into();
        s.detail = detail.into();
    };

    let transport = AndroidTransport {
        out: inner.out.clone(),
        in_tx: inner.in_tx.clone(),
    };
    let client = OuraClient::new(transport);
    let key = inner.key;
    let store = inner.store.clone();

    // 1/5 installation de la clé (anneau keyless → SetAuthKey sans auth préalable)
    set("ceremony", "1/5 installation de la clé…");
    let serial = client.serial().await.unwrap_or_else(|_| "unknown".into());
    status.lock().unwrap().serial = serial.clone();
    // Si une tentative précédente a déjà posé la clé, SetAuthKey échouera :
    // ce n'est pas fatal, on vérifie ensuite par l'authentification.
    let key_set = match client.set_auth_key(&key).await {
        Ok(()) => "OK",
        Err(e) => {
            set("ceremony", &format!("set_auth_key KO ({e}) → test de l'auth…"));
            "déjà posée ?"
        }
    };

    // 2/5 vérification de l'auth avec la clé installée
    set("ceremony", "2/5 authentification…");
    match client.authenticate(&key).await {
        Ok(AuthResult::Success) => {}
        Ok(a) => {
            set("error", &format!("auth refusée : {a:?} (set_auth_key {key_set})"));
            return;
        }
        Err(e) => {
            set("error", &format!("auth: {e} (set_auth_key {key_set})"));
            return;
        }
    }
    status.lock().unwrap().auth = "success".into();

    // 3/5 setup + time-sync + features HR/SpO2 + sleep-analyze
    set("ceremony", "3/5 setup + time-sync…");
    if let Err(e) = client.setup_app_stream().await {
        set("error", &format!("setup: {e}"));
        return;
    }
    if let Err(e) = client.sync_time_app().await {
        set("error", &format!("sync_time: {e}"));
        return;
    }
    let info = client.firmware().await.ok();
    let battery = client.battery().await.ok();
    {
        let mut s = status.lock().unwrap();
        if let Some(b) = &battery {
            s.battery = b.percent as i64;
        }
    }
    set("ceremony", "4/5 features HR/SpO2 + sleep-analyze…");
    let feats = client
        .set_feature_mode(feature::DAYTIME_HR, feature_mode::AUTOMATIC)
        .await
        .map(|()| "HR")
        .err();
    let spo2 = client
        .set_feature_mode(feature::SPO2, feature_mode::AUTOMATIC)
        .await
        .map(|()| "SpO2")
        .err();
    let sleep = client.check_sleep_analysis(true).await.err();
    // non fatals : la sync passe quand même
    let feat_detail = format!(
        "features {} {} {}",
        feats.map(|e| format!("KO ({e})")).unwrap_or_else(|| "OK".into()),
        spo2.map(|e| format!("KO ({e})")).unwrap_or_else(|| "OK".into()),
        sleep.map(|e| format!("KO ({e})")).unwrap_or_else(|| "OK".into()),
    );
    {
        let st = store.lock().unwrap();
        let _ = st.upsert_device(&serial, None, info.as_ref());
    }

    // 4/5 drain complet
    set("ceremony", "5/5 drain des événements…");
    let cursor = store.lock().unwrap().cursor(&serial).unwrap_or(0);
    let pending: Mutex<Vec<oura_protocol::events::RingEvent>> = Mutex::new(Vec::new());
    let outcome = match client
        .drain_events(
            cursor,
            |ev| {
                pending.lock().unwrap().push(ev.clone());
                true
            },
            |p| {
                let events = std::mem::take(&mut *pending.lock().unwrap());
                let st = store.lock().unwrap();
                match st.commit_batch(&serial, &events, p.next_cursor) {
                    Ok(n) => {
                        let mut s = status.lock().unwrap();
                        s.cursor = p.next_cursor as i64;
                        s.bytes_left = p.bytes_left as i64;
                        s.events = p.events_synced as i64;
                        s.inserted += n as i64;
                    }
                    Err(e) => {
                        set("error", &format!("db: {e}"));
                        return false;
                    }
                }
                true
            },
        )
        .await
    {
        Ok(o) => o,
        Err(e) => {
            set("error", &format!("drain: {e}"));
            return;
        }
    };
    if let Err(e) = store.lock().unwrap().set_cursor(&serial, outcome.next_cursor) {
        set("error", &format!("curseur final: {e}"));
        return;
    }
    let s = status.lock().unwrap();
    let detail = format!(
        "clé installée, {} — {} événements, curseur {}",
        feat_detail, outcome.events_synced, outcome.next_cursor
    );
    drop(s);
    set("done", &detail);
}

// ---------------------------------------------------------------------------
// API FFI (C ABI)
// ---------------------------------------------------------------------------

fn to_bytes(v: Vec<u8>) -> *mut u8 {
    let b: Box<[u8]> = v.into_boxed_slice();
    let p: *mut [u8] = Box::into_raw(b);
    p as *mut u8
}

/// Crée le core. `key_hex` = 32 hex digits (clé 16 o), `db_path` = chemin SQLite.
/// Renvoie null si échec (message sur stderr, visible dans logcat).
#[no_mangle]
pub extern "C" fn core_create(
    key_hex: *const u8,
    key_len: i32,
    db_path: *const u8,
    db_len: i32,
) -> *mut Core {
    let result = (|| -> anyhow::Result<Core> {
        let key_bytes = unsafe { std::slice::from_raw_parts(key_hex, key_len as usize) };
        let db_bytes = unsafe { std::slice::from_raw_parts(db_path, db_len as usize) };
        let key_dec = hex::decode(key_bytes).with_context(|| "clé hex invalide")?;
        let key: [u8; 16] = key_dec
            .as_slice()
            .try_into()
            .map_err(|_| anyhow::anyhow!("clé != 16 octets"))?;
        let path = OsStr::from_bytes(db_bytes);
        let store = Store::open(path)?;
        // multi-thread obligatoire : on utilise `spawn()` sans `block_on`,
        // or un runtime current-thread ne poll ses tâches que dans `block_on`.
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()?;
        let (in_tx, _) = tokio::sync::broadcast::channel(2048);
        Ok(Core {
            runtime,
            inner: Arc::new(Inner {
                out: Arc::new(Mutex::new(VecDeque::new())),
                in_tx,
                store: Arc::new(Mutex::new(store)),
                status: Arc::new(Mutex::new(Status::default())),
                key,
            }),
        })
    })();
    match result {
        Ok(core) => Box::into_raw(Box::new(core)),
        Err(e) => {
            eprintln!("oura_core_create: {e:#}");
            std::ptr::null_mut()
        }
    }
}

/// Lance le flux de sync (l'app doit être déjà connectée + notifier).
#[no_mangle]
pub extern "C" fn core_start_sync(core: *mut Core) {
    let core = unsafe { &*core };
    let inner = core.inner.clone();
    reset_status(&inner, "auth", "démarrage…");
    core.runtime.spawn(run_sync(inner));
}

/// Remet le statut à zéro **de façon synchrone** avant de lancer une tâche.
/// Sinon Kotlin, qui sonde le statut dès le retour de cette fonction, lit
/// encore l'état terminal du cycle précédent ("done"/"error") et conclut à
/// tort avant même que la nouvelle tâche ait été pollée.
fn reset_status(inner: &Arc<Inner>, state: &str, detail: &str) {
    let mut s = inner.status.lock().unwrap();
    s.state = state.into();
    s.detail = detail.into();
    s.auth = String::new();
    s.events = 0;
    s.inserted = 0;
    s.bytes_left = 0;
}

/// Cérémonie de bascule (factory-reset → clé → features → sync complète).
#[no_mangle]
pub extern "C" fn core_start_ceremony(core: *mut Core) {
    let core = unsafe { &*core };
    let inner = core.inner.clone();
    reset_status(&inner, "ceremony", "démarrage…");
    core.runtime.spawn(run_ceremony(inner));
}

/// Notification GATT reçue → alimente la machine à états.
#[no_mangle]
pub extern "C" fn core_feed(core: *mut Core, data: *const u8, len: i32) {
    let core = unsafe { &*core };
    let bytes = unsafe { std::slice::from_raw_parts(data, len as usize) };
    let _ = core.inner.in_tx.send(bytes.to_vec());
}

/// Prochain paquet à écrire sur la caractéristique write (null si vide).
/// Le tampon renvoyé est libéré par `core_free_buf`.
#[no_mangle]
pub extern "C" fn core_next_write(core: *mut Core, len_out: *mut i32) -> *mut u8 {
    let core = unsafe { &*core };
    let pkt = core.inner.out.lock().unwrap().pop_front();
    match pkt {
        Some(p) => {
            if !len_out.is_null() {
                unsafe { *len_out = p.len() as i32 };
            }
            to_bytes(p)
        }
        None => {
            if !len_out.is_null() {
                unsafe { *len_out = 0 };
            }
            std::ptr::null_mut()
        }
    }
}

/// État JSON : state/detail/serial/auth/events/inserted/cursor/bytes_left/battery.
#[no_mangle]
pub extern "C" fn core_status(core: *mut Core, len_out: *mut i32) -> *mut u8 {
    let core = unsafe { &*core };
    let b = core.inner.status.lock().unwrap().json().into_bytes();
    if !len_out.is_null() {
        unsafe { *len_out = b.len() as i32 };
    }
    to_bytes(b)
}

/// Delta à pousser : `limit` événements les plus récents (plus récents d'abord).
/// JSON : [{"serial","tag","name","ring_timestamp","body_hex","decoded_json","captured_unix"}]
#[no_mangle]
pub extern "C" fn core_recent_events(core: *mut Core, limit: i32, len_out: *mut i32) -> *mut u8 {
    let core = unsafe { &*core };
    let serial = core.inner.status.lock().unwrap().serial.clone();
    let rows = {
        let st = core.inner.store.lock().unwrap();
        st.recent_events(&serial, limit as i64)
    };
    match rows {
        Ok(rows) => {
            let arr: Vec<serde_json::Value> = rows
                .into_iter()
                .map(|(serial, tag, name, ring_ts, body_hex, decoded, captured)| {
                    let decoded_value = decoded
                        .as_ref()
                        .and_then(|d| serde_json::from_str::<serde_json::Value>(d).ok())
                        .unwrap_or_else(|| serde_json::Value::Null);
                    serde_json::json!({
                        "serial": serial,
                        "tag": tag,
                        "name": name,
                        "ring_timestamp": ring_ts,
                        "body_hex": body_hex,
                        "decoded_json": decoded_value,
                        "captured_unix": captured,
                    })
                })
                .collect();
            let b = serde_json::to_string(&arr).unwrap_or_else(|_| "[]".into()).into_bytes();
            if !len_out.is_null() {
                unsafe { *len_out = b.len() as i32 };
            }
            to_bytes(b)
        }
        Err(e) => {
            eprintln!("oura_recent_events: {e:#}");
            let b = "[]".as_bytes().to_vec();
            if !len_out.is_null() {
                unsafe { *len_out = b.len() as i32 };
            }
            to_bytes(b)
        }
    }
}

/// Libère un tampon de longueur connue.
#[no_mangle]
pub extern "C" fn core_free_buf_len(ptr: *mut u8, len: i32) {
    if !ptr.is_null() && len > 0 {
        unsafe { drop(Box::from_raw(std::slice::from_raw_parts_mut(ptr, len as usize))) };
    }
}

// ---------------------------------------------------------------------------
// Wrapper JNI (classe Kotlin io.github.paindespik.ourascan.Core)
// ---------------------------------------------------------------------------

use jni::objects::{JByteArray, JClass, JString};
use jni::sys::jlong;
use jni::JNIEnv;

fn jbytes(env: &JNIEnv, arr: &JByteArray) -> Option<Vec<u8>> {
    let n = env.get_array_length(arr).ok()? as usize;
    let mut buf = vec![0i8; n];
    env.get_byte_array_region(arr, 0, &mut buf).ok()?;
    Some(buf.iter().map(|b| *b as u8).collect())
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeCreate<'local>(
    env: JNIEnv<'local>,
    _cls: JClass<'local>,
    key: JByteArray<'local>,
    db: JByteArray<'local>,
) -> jlong {
    let (kb, db_) = match (jbytes(&env, &key), jbytes(&env, &db)) {
        (Some(k), Some(d)) => (k, d),
        _ => return 0,
    };
    let r = core_create(kb.as_ptr(), kb.len() as i32, db_.as_ptr(), db_.len() as i32);
    r as jlong
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeStartSync<'local>(
    _env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong,
) {
    core_start_sync(ptr as *mut Core);
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeStartCeremony<'local>(
    _env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong,
) {
    core_start_ceremony(ptr as *mut Core);
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeFeed<'local>(
    env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong, data: JByteArray<'local>,
) {
    let d = match jbytes(&env, &data) {
        Some(d) => d, None => return,
    };
    core_feed(ptr as *mut Core, d.as_ptr(), d.len() as i32);
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeNextWrite<'local>(
    env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong,
) -> jni::sys::jobject {
    let mut len = 0i32;
    let p = core_next_write(ptr as *mut Core, &mut len);
    if p.is_null() {
        return std::ptr::null_mut();
    }
    let bytes = unsafe { std::slice::from_raw_parts(p, len as usize) };
    let arr = match env.new_byte_array(len as jni::sys::jsize) {
        Ok(a) => a, Err(_) => { core_free_buf_len(p, len); return std::ptr::null_mut(); }
    };
    let ibytes = unsafe { std::slice::from_raw_parts(bytes.as_ptr() as *const i8, bytes.len()) };
    let _ = env.set_byte_array_region(&arr, 0, ibytes);
    core_free_buf_len(p, len);
    arr.into_raw()
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeStatus<'local>(
    env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong,
) -> JString<'local> {
    let mut len = 0i32;
    let p = core_status(ptr as *mut Core, &mut len);
    let s = unsafe { std::slice::from_raw_parts(p, len as usize) };
    let out = String::from_utf8_lossy(s).into_owned();
    core_free_buf_len(p, len);
    env.new_string(out).unwrap_or_else(|_| JString::default())
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeRecentEvents<'local>(
    env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong, limit: i32,
) -> JString<'local> {
    let mut len = 0i32;
    let p = core_recent_events(ptr as *mut Core, limit, &mut len);
    let s = unsafe { std::slice::from_raw_parts(p, len as usize) };
    let out = String::from_utf8_lossy(s).into_owned();
    core_free_buf_len(p, len);
    env.new_string(out).unwrap_or_else(|_| JString::default())
}

#[no_mangle]
pub extern "system" fn Java_io_github_paindespik_ourascan_Core_nativeDrop<'local>(
    _env: JNIEnv<'local>, _cls: JClass<'local>, ptr: jlong,
) {
    core_drop(ptr as *mut Core);
}

/// Détruit le core (à appeler une fois, en fin de vie du process app).
#[no_mangle]
pub extern "C" fn core_drop(core: *mut Core) {
    if !core.is_null() {
        unsafe { drop(Box::from_raw(core)) };
    }
}
