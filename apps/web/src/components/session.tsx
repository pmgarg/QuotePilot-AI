"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { SessionInfo } from "@/lib/types";

interface SessionContextValue {
  session: SessionInfo | null;
  loading: boolean;
  refresh: () => Promise<void>;
  can: (permission: string) => boolean;
  logout: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue>({ session: null, loading: true, refresh: async () => {}, can: () => false, logout: async () => {} });

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();
  const pathname = usePathname();

  const refresh = useCallback(async () => {
    try {
      const me = await api<SessionInfo>("/auth/me");
      setSession(me);
    } catch {
      setSession(null);
      if (!pathname.startsWith("/login")) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    } finally {
      setLoading(false);
    }
  }, [pathname, router]);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const can = useCallback((p: string) => session?.permissions.includes(p) ?? false, [session]);
  const logout = useCallback(async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setSession(null);
      router.replace("/login");
    }
  }, [router]);

  return <SessionContext.Provider value={{ session, loading, refresh, can, logout }}>{children}</SessionContext.Provider>;
}

export function useSession() {
  return useContext(SessionContext);
}
