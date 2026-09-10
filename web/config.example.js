// Copy this file to web/config.js and fill in your project's values.
// web/config.js is gitignored.
//
// Use the ANON / PUBLISHABLE key ONLY. The service_role key bypasses RLS and
// must NEVER appear in any file served to a browser.
window.GSS_CONFIG = {
  SUPABASE_URL: "https://YOUR-PROJECT-REF.supabase.co",
  SUPABASE_ANON_KEY: "YOUR-ANON-OR-PUBLISHABLE-KEY",
  // The drone row to watch. Matches DRONE_ID in gss/config.py / .env.
  DRONE_ID: "d5030000-0000-4000-8000-000000000001",
};
