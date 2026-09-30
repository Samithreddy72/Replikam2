export type HistoryEntry = { started: string; ended: string; bridge: string };
export const PAGE_SIZE = 8;
let connection: Promise<IDBDatabase> | undefined;
const request = <T>(r: IDBRequest<T>) => new Promise<T>((resolve, reject) => {
  r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
});
const done = (t: IDBTransaction) => new Promise<void>((resolve, reject) => {
  t.oncomplete = () => resolve(); t.onerror = t.onabort = () => reject(t.error || new Error("Session history could not be saved."));
});
async function database() {
  if (!connection) connection = (async () => {
    const r = indexedDB.open("netbridge-history", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("sessions", {keyPath: "started"});
    const db = await request(r);
    // One-time migration; retain the old copy until the write succeeds.
    const legacy = localStorage.getItem("nb.sessions");
    if (legacy) {
      const entries = JSON.parse(legacy);
      const t = db.transaction("sessions", "readwrite"); const complete = done(t);
      if (Array.isArray(entries)) for (const entry of entries) {
        if (typeof entry.started === "string" && typeof entry.ended === "string" && typeof entry.bridge === "string") t.objectStore("sessions").put(entry);
      }
      await complete; localStorage.removeItem("nb.sessions");
    }
    return db;
  })().catch(e => { connection = undefined; throw e; });
  return connection;
}
export async function saveSession(entry: HistoryEntry) {
  const db = await database(), t = db.transaction("sessions", "readwrite"), complete = done(t);
  t.objectStore("sessions").put(entry); await complete;
}
export async function sessionPage(page: number) {
  const db = await database(), store = db.transaction("sessions").objectStore("sessions");
  const totalPromise = request(store.count());
  const entries = await new Promise<HistoryEntry[]>((resolve, reject) => {
    const result: HistoryEntry[] = []; const r = store.openCursor(null, "prev"); let skipped = false;
    r.onerror = () => reject(r.error);
    r.onsuccess = () => {
      const cursor = r.result;
      if (!cursor || result.length === PAGE_SIZE) { resolve(result); return; }
      if (!skipped && page > 0) { skipped = true; cursor.advance(page * PAGE_SIZE); return; }
      result.push(cursor.value); if (result.length === PAGE_SIZE) resolve(result); else cursor.continue();
    };
  });
  return {entries, total: await totalPromise};
}
