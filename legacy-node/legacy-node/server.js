// =============================================================
// MAYA backend API (section 10 of the spec)
// Small Node/Express service for Render's free tier.
// The Android app never calls Gemini or the database directly -
// it calls this API. The Gemini key and the Supabase
// service-role key live here only, as environment variables.
// =============================================================
import express from "express";
import { createClient } from "@supabase/supabase-js";

const {
  SUPABASE_URL,
  SUPABASE_SERVICE_ROLE_KEY, // server-side only, NEVER in the APK
  GEMINI_API_KEY,            // server-side only, NEVER in the APK
  GEMINI_MODEL = "gemini-2.0-flash",
  PORT = 3000,
} = process.env;

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY || !GEMINI_API_KEY) {
  console.error("Missing env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GEMINI_API_KEY are required.");
  process.exit(1);
}

// Service-role client: bypasses RLS, so EVERY query below is
// explicitly scoped with .eq("user_id", user.id). Never expose
// this key to the app.
const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

const app = express();
app.use(express.json({ limit: "256kb" }));

// -------------------------------------------------------------
// Health check - Render + uptime monitors ping this.
// -------------------------------------------------------------
app.get("/health", (req, res) => {
  res.status(200).json({ ok: true, service: "maya-backend", time: new Date().toISOString() });
});

// -------------------------------------------------------------
// Auth: the app sends the user's Supabase sign-in token as
// "Authorization: Bearer <token>". We verify it and get the
// user. The anon key never reaches this server.
// -------------------------------------------------------------
async function requireUser(req, res, next) {
  try {
    const header = req.headers.authorization || "";
    const token = header.startsWith("Bearer ") ? header.slice(7) : null;
    if (!token) return res.status(401).json({ error: "missing_token" });
    const { data, error } = await supabase.auth.getUser(token);
    if (error || !data?.user) return res.status(401).json({ error: "invalid_token" });
    req.user = data.user;
    next();
  } catch (err) {
    next(err);
  }
}

// -------------------------------------------------------------
// POST /v1/chat - the main thinking loop.
// Body: { message: string }
// 1. Recall this user's recent memories.
// 2. Ask Gemini (key stays here).
// 3. Save both lines to this user's conversation log.
// -------------------------------------------------------------
app.post("/v1/chat", requireUser, async (req, res, next) => {
  try {
    const message = (req.body?.message || "").toString().trim();
    if (!message) return res.status(400).json({ error: "message_required" });

    const { data: memories } = await supabase
      .from("memories")
      .select("kind, content")
      .eq("user_id", req.user.id)
      .order("last_used_at", { ascending: false, nullsFirst: false })
      .limit(20);

    const memoryBlock = (memories || [])
      .map((m) => `- (${m.kind}) ${m.content}`)
      .join("\n");

    const prompt =
      "You are Maya, the owner's personal agent.\n" +
      (memoryBlock ? `What you remember about the owner:\n${memoryBlock}\n\n` : "") +
      `Owner says: ${message}`;

    const geminiRes = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY },
        body: JSON.stringify({ contents: [{ role: "user", parts: [{ text: prompt }] }] }),
      }
    );
    if (!geminiRes.ok) {
      // Status only - never log the response body (may echo the prompt).
      console.error(`Gemini call failed: HTTP ${geminiRes.status}`);
      return res.status(502).json({ error: "gemini_unavailable" });
    }
    const geminiData = await geminiRes.json();
    const reply =
      geminiData?.candidates?.[0]?.content?.parts?.map((p) => p.text).join("")?.trim() ||
      "...";

    await supabase.from("conversations").insert([
      { user_id: req.user.id, role: "user", text: message },
      { user_id: req.user.id, role: "maya", text: reply },
    ]);

    res.json({ reply });
  } catch (err) {
    next(err);
  }
});

// -------------------------------------------------------------
// Memories - recall, save, forget (all scoped to the caller).
// -------------------------------------------------------------
app.get("/v1/memories", requireUser, async (req, res, next) => {
  try {
    const { data, error } = await supabase
      .from("memories")
      .select("id, kind, content, source, created_at, last_used_at")
      .eq("user_id", req.user.id)
      .order("created_at", { ascending: false });
    if (error) throw error;
    res.json({ memories: data });
  } catch (err) { next(err); }
});

app.post("/v1/memories", requireUser, async (req, res, next) => {
  try {
    const { kind, content, source } = req.body || {};
    if (!kind || !content) return res.status(400).json({ error: "kind_and_content_required" });
    const { data, error } = await supabase
      .from("memories")
      .insert({ user_id: req.user.id, kind, content, source: source || "chat" })
      .select("id")
      .single();
    if (error) throw error;
    res.status(201).json({ id: data.id });
  } catch (err) { next(err); }
});

app.delete("/v1/memories/:id", requireUser, async (req, res, next) => {
  try {
    // "Forget completely": hard delete, no archive.
    const { error } = await supabase
      .from("memories")
      .delete()
      .eq("id", req.params.id)
      .eq("user_id", req.user.id);
    if (error) throw error;
    res.status(204).end();
  } catch (err) { next(err); }
});

// -------------------------------------------------------------
// Tasks - ongoing work, scoped to the caller.
// -------------------------------------------------------------
app.get("/v1/tasks", requireUser, async (req, res, next) => {
  try {
    const { data, error } = await supabase
      .from("tasks")
      .select("id, title, status, due, notes, created_at, updated_at")
      .eq("user_id", req.user.id)
      .order("created_at", { ascending: false });
    if (error) throw error;
    res.json({ tasks: data });
  } catch (err) { next(err); }
});

app.post("/v1/tasks", requireUser, async (req, res, next) => {
  try {
    const { title, due, notes } = req.body || {};
    if (!title) return res.status(400).json({ error: "title_required" });
    const { data, error } = await supabase
      .from("tasks")
      .insert({ user_id: req.user.id, title, due: due || null, notes: notes || null })
      .select("id")
      .single();
    if (error) throw error;
    res.status(201).json({ id: data.id });
  } catch (err) { next(err); }
});

// -------------------------------------------------------------
// 404 + error handling.
// Logging rule from the spec: enough detail to debug, but NEVER
// keys, tokens, or user message bodies - so we log only the
// method, path, status and the error message.
// -------------------------------------------------------------
app.use((req, res) => res.status(404).json({ error: "not_found" }));

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error(`${req.method} ${req.path} -> 500: ${err.message}`);
  res.status(500).json({ error: "internal_error" });
});

app.listen(PORT, () => {
  console.log(`maya-backend listening on :${PORT}`);
});
