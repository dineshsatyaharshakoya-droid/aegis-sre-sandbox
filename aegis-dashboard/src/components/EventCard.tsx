import { ShieldAlert, Cpu, Code2, CheckCircle2, AlertTriangle } from 'lucide-react';
import type { AegisEvent } from '../types';

interface EventCardProps {
  event: AegisEvent;
  onClick: (event: AegisEvent) => void;
  isSelected: boolean;
}

export const EventCard = ({ event, onClick, isSelected }: EventCardProps) => {
  const renderIcon = (type: string) => {
    switch(type) {
      case 'telemetry_received': return <AlertTriangle size={14} />;
      case 'node_update': return <Cpu size={14} />;
      case 'patch_ready': return <Code2 size={14} />;
      case 'patch_deployed': return <CheckCircle2 size={14} />;
      default: return <ShieldAlert size={14} />;
    }
  };

  const getTitle = () => {
    switch(event.type) {
      case 'telemetry_received': return 'CRASH DETECTED';
      case 'node_update': return event.node ? `NODE: ${event.node.toUpperCase()}` : 'NODE UPDATE';
      case 'patch_ready': return 'PATCH PROPOSED';
      case 'patch_deployed': return 'DEPLOYED TO PROD';
      case 'error': return 'SYSTEM FAULT';
      default: return 'UNKNOWN EVENT';
    }
  };

  const getDescription = () => {
    switch(event.type) {
      case 'telemetry_received': return event.service;
      case 'node_update': return event.state_summary?.join(', ');
      case 'patch_ready': return event.file;
      case 'patch_deployed': return `Human Verified: ${event.file}`;
      case 'error': return event.message;
      default: return '';
    }
  };

  return (
    <div 
      className={`event-card ${event.type} ${isSelected ? 'selected' : ''}`}
      onClick={() => onClick(event)}
      style={{ cursor: 'pointer' }}
    >
      <div className="event-icon-wrapper">
        {renderIcon(event.type)}
      </div>
      <div className="event-content">
        <div className="event-header">
          <span className="event-title">{getTitle()}</span>
          <span className="event-time">{event.time}</span>
        </div>
        <div className="event-desc">{getDescription()}</div>
      </div>
    </div>
  );
};
