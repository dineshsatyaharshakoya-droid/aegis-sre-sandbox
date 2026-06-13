import { AlertTriangle, Cpu, TerminalSquare, Tag } from 'lucide-react';
import type { AegisEvent } from '../types';

interface EventDetailsProps {
  event: AegisEvent;
}

export const EventDetails = ({ event }: EventDetailsProps) => {
  if (event.type === 'telemetry_received') {
    return (
      <div className="event-details-panel">
        <div className="detail-card">
          <div className="detail-header error">
            <AlertTriangle size={18} />
            <h3>Crash Telemetry Intercepted</h3>
          </div>
          
          <div className="detail-property">
            <span className="label">Target Service</span>
            <span className="badge badge-error">{event.service || 'Unknown'}</span>
          </div>

          <div className="detail-property">
            <span className="label">Timestamp</span>
            <span className="value monospace">{event.time}</span>
          </div>
          
          <div className="log-container">
            <div className="log-header">
              <TerminalSquare size={14} />
              <span>Raw Stack Trace</span>
            </div>
            <pre className="log-body error-log">{event.crash}</pre>
          </div>
        </div>
      </div>
    );
  }

  if (event.type === 'node_update') {
    return (
      <div className="event-details-panel">
        <div className="detail-card">
          <div className="detail-header info">
            <Cpu size={18} />
            <h3>LangGraph Node Execution</h3>
          </div>
          
          <div className="detail-property">
            <span className="label">Active Node</span>
            <span className="badge badge-info">{event.node?.toUpperCase()}</span>
          </div>

          <div className="detail-property">
            <span className="label">Execution Time</span>
            <span className="value monospace">{event.time}</span>
          </div>

          <div className="state-container">
            <div className="state-header">
              <Tag size={14} />
              <span>Mutated Graph State Keys</span>
            </div>
            <div className="pill-group">
              {event.state_summary?.map((key) => (
                <span key={key} className="pill">{key}</span>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="terminal-window">
      <div className="terminal-header"><span className="terminal-title">System Event</span></div>
      <div className="terminal-body">
        <pre style={{ margin: 0 }}>{JSON.stringify(event, null, 2)}</pre>
      </div>
    </div>
  );
};
