import { useEffect, useRef } from 'react';
import { ShieldCheck, AlertTriangle } from 'lucide-react';
import type { Incident } from '../types';

interface SidebarProps {
  incidents: Incident[];
  connected: boolean;
  isOpen: boolean;
  onIncidentClick: (incident: Incident) => void;
  selectedIncidentId: string | null;
}

export const Sidebar = ({ incidents, connected, isOpen, onIncidentClick, selectedIncidentId }: SidebarProps) => {
  const eventsEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [incidents]);

  return (
    <aside className={`sidebar ${isOpen ? 'mobile-open' : ''}`}>
      <div className="sidebar-header">
        <h1 className="logo">AEGIS <span>SRE</span></h1>
        <div className={`status-badge ${connected ? 'live' : 'offline'}`}>
          <div className="pulse-dot"></div>
          {connected ? 'LIVE' : 'OFFLINE'}
        </div>
      </div>

      <div className="feed-container">
        {incidents.map(inc => (
          <div 
            key={inc.id}
            className={`event-card ${inc.status} ${inc.id === selectedIncidentId ? 'selected' : ''}`}
            onClick={() => onIncidentClick(inc)}
            style={{ cursor: 'pointer' }}
          >
            <div className="event-icon-wrapper">
              <AlertTriangle size={14} />
            </div>
            <div className="event-content">
              <div className="event-title">{inc.service}</div>
              <div className="event-time">{inc.time} • {inc.status.toUpperCase()}</div>
            </div>
          </div>
        ))}
        <div ref={eventsEndRef} />
      </div>
    </aside>
  );
};
