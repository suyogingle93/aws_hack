/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Control API base URL (default http://127.0.0.1:8000). */
  readonly VITE_HARNESS_API?: string;
}
