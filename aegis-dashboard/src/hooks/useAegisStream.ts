import { useState, useEffect, useRef, useCallback } from 'react';
import type { AegisEvent, Incident } from '../types';

export const useAegisStream = (url: string) => {
  const [incidents, setIncidents] = useState<Record<string, Incident>>({});
  const [connected, setConnected] = useState(false);
  const ws = useRef<WebSocket | null>(null);

  useEffect(() => {
    // 1. Hydrate history on mount
    const fetchHistory = async () => {
      try {
        const res = await fetch(url.replace('ws://', 'http://').replace('wss://', 'https://').replace('/ws', '/incidents'));
        if (res.ok) {
          const data = await res.json();
          const historyIncidents: Record<string, Incident> = {};
          data.incidents.forEach((inc: any) => {
            historyIncidents[inc.id] = {
              id: inc.id,
              service: inc.service,
              time: new Date(inc.created_at * 1000).toLocaleTimeString(),
              status: inc.status === 'completed' ? 'deployed' : 'investigating',
              nodes_executed: inc.status === 'completed' ? ['planner', 'researcher', 'executor', 'sandbox', 'reviewer'] : [],
              crash_log: inc.crash_log
            };
          });
          setIncidents(prev => ({...historyIncidents, ...prev}));
        }
      } catch (e) {
        console.error("Failed to hydrate history", e);
      }
    };
    fetchHistory();

    // 2. WebSocket Connection with Exponential Backoff Reconnect
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let attempt = 0;

    const connectWs = () => {
      ws.current = new WebSocket(url);
      
      ws.current.onopen = () => {
        setConnected(true);
        attempt = 0; // Reset backoff
      };
      
      ws.current.onclose = () => {
        setConnected(false);
        // Exponential backoff reconnect
        const delay = Math.min(1000 * Math.pow(2, attempt), 30000);
        attempt++;
        reconnectTimeout = setTimeout(connectWs, delay);
      };
    
    ws.current.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        const event: AegisEvent = {
          ...data,
          id: Math.random().toString(36).substring(7),
          time: new Date().toLocaleTimeString()
        };
        
        if (!event.incident_id) return;

        setIncidents(prev => {
          const incId = event.incident_id!;
          const existing = prev[incId] || {
            id: incId,
            service: event.service || 'Unknown System',
            time: event.time,
            status: 'investigating',
            nodes_executed: []
          };

          const updated = { ...existing };

          if (event.type === 'telemetry_received') {
            updated.crash_log = event.crash;
            updated.service = event.service || updated.service;
          } else if (event.type === 'node_update' && event.node) {
            if (!updated.nodes_executed.includes(event.node)) {
              updated.nodes_executed = [...updated.nodes_executed, event.node];
            }
          } else if (event.type === 'patch_ready') {
            updated.status = 'patch_ready';
            updated.patch = {
              file: event.file || '',
              diff: event.diff || '',
              root_cause_analysis: event.root_cause_analysis || '',
              explanation: event.explanation || ''
            };
          } else if (event.type === 'patch_deployed') {
            updated.status = 'deployed';
          } else if (event.type === 'patch_rejected') {
            updated.status = 'rejected';
          } else if (event.type === 'error') {
            updated.status = 'error';
          }

          return { ...prev, [incId]: updated };
        });
        
      } catch (e) {
        console.error("Failed to parse websocket message", e);
      }
    };
    }; // End of connectWs

    connectWs();

    return () => {
      clearTimeout(reconnectTimeout);
      if (ws.current) {
        ws.current.onclose = null; // Prevent reconnect loop on unmount
        ws.current.close();
      }
    };
  }, [url]);

  const approvePatch = useCallback((incidentId: string | undefined) => {
    if (ws.current && ws.current.readyState === WebSocket.OPEN && incidentId) {
      // Backend expects incident_id (was previously sending `file`, which the
      // server rejected with "approve_patch requires incident_id").
      ws.current.send(JSON.stringify({ action: 'approve_patch', incident_id: incidentId }));
    }
  }, []);

  const rejectPatch = useCallback((incidentId: string | undefined) => {
    if (ws.current && ws.current.readyState === WebSocket.OPEN && incidentId) {
      ws.current.send(JSON.stringify({ action: 'reject_patch', incident_id: incidentId }));
    }
  }, []);

  return { incidents, connected, approvePatch, rejectPatch };
};
