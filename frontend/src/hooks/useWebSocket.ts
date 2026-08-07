import { useEffect, useRef } from "react";
import type { WsEvent } from "../api/types";

export function useWebSocket(onEvent: (ev: WsEvent) => void): void {
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (typeof window === "undefined") return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/v1/events`;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(url);
      ws.onmessage = (e) => {
        try {
          const parsed = JSON.parse(e.data) as WsEvent;
          onEventRef.current(parsed);
        } catch {
          // ignore malformed
        }
      };
      ws.onclose = () => {
        if (closed) return;
        // reconnect after 3s
        reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => { ws?.close(); };
    };
    connect();

    return () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);
}
