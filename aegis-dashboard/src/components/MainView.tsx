import { BrainCircuit, Rocket, X, ShieldCheck, Menu, Bell, Settings, TerminalSquare, AlertTriangle } from 'lucide-react';
import type { Incident } from '../types';
import { PatchTerminal } from './PatchTerminal';

interface MainViewProps {
  incident: Incident | null;
  incidentCount: number;
  onApprove: (file: string | undefined) => void;
  onReject: () => void;
  toggleSidebar: () => void;
}

const ALL_NODES = ['planner', 'researcher', 'executor', 'sandbox', 'reviewer'];

export const MainView = ({ incident, incidentCount, onApprove, onReject, toggleSidebar }: MainViewProps) => {

  return (
    <main className="main-area functional-layout">
      <header className="top-nav functional-nav">
        <div className="nav-left">
          <button className="mobile-menu-btn" onClick={toggleSidebar}>
            <Menu size={18} />
          </button>
          <div className="nav-tabs">
            <div className="tab active">Incident Matrix</div>
            <div className="tab">Global Mesh</div>
            <div className="tab">Policy Rules</div>
          </div>
        </div>
        <div className="nav-actions">
          <button className="icon-btn"><Bell size={16} /></button>
          <button className="icon-btn"><Settings size={16} /></button>
          <div className="user-avatar-small"></div>
        </div>
      </header>

      <div className="kpi-bar">
        <div className="kpi-item">
          <span className="kpi-label">System Status</span>
          <span className="kpi-value success">NOMINAL</span>
        </div>
        <div className="kpi-item">
          <span className="kpi-label">Active Incidents</span>
          <span className={`kpi-value ${incidentCount > 0 ? 'warning' : 'success'}`}>
            {incidentCount}
          </span>
        </div>
        <div className="kpi-item">
          <span className="kpi-label">Avg MTTR</span>
          <span className="kpi-value">14m 20s</span>
        </div>
      </div>

      {incident ? (
        <div className="dashboard-grid">
          
          {/* LEFT COLUMN: Context & Diagnostics */}
          <div className="dashboard-pane context-pane">
            <div className="pane-header">
              <div className="header-meta">INCIDENT: {incident.id}</div>
              <h2 className="service-title">{incident.service}</h2>
              <div className={`status-badge-inline ${incident.status}`}>
                {incident.status.toUpperCase()}
              </div>
            </div>

            <div className="data-card log-card">
              <div className="data-header">
                <TerminalSquare size={14} />
                <span>Crash Telemetry</span>
              </div>
              <pre className="raw-log">{incident.crash_log}</pre>
            </div>

            <div className="data-card">
              <div className="data-header">
                <BrainCircuit size={14} />
                <span>Swarm Execution Pipeline</span>
              </div>
              <div className="dense-pipeline">
                {ALL_NODES.map(node => {
                  const isActive = incident.nodes_executed.includes(node);
                  return (
                    <div key={node} className={`dense-node ${isActive ? 'active' : ''}`}>
                      <div className="node-indicator"></div>
                      <span className="node-name">{node}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {incident.status === 'patch_ready' && incident.patch && (
              <>
                <div className="data-card rca-card">
                  <div className="data-header error-header">
                    <AlertTriangle size={14} />
                    <span>Root Cause Analysis</span>
                  </div>
                  <p className="rca-text">{incident.patch.root_cause_analysis}</p>
                </div>
                
                <div className="data-card sra-card">
                  <div className="data-header insight-header">
                    <ShieldCheck size={14} />
                    <span>SRA Insight</span>
                  </div>
                  <p className="sra-text">{incident.patch.explanation}</p>
                </div>
              </>
            )}
          </div>

          {/* RIGHT COLUMN: Action & Code Patch */}
          <div className="dashboard-pane patch-pane">
            <div className="pinned-action-bar">
              <div className="action-title">
                Proposed Patch: <code>{incident.patch?.file || 'Pending...'}</code>
              </div>
              <div className="action-buttons">
                <button 
                  className="btn-dense reject" 
                  onClick={onReject}
                  disabled={incident.status !== 'patch_ready'}
                >
                  <X size={14} /> Discard
                </button>
                <button 
                  className="btn-dense approve" 
                  onClick={() => onApprove(incident.patch?.file)}
                  disabled={incident.status !== 'patch_ready'}
                >
                  <Rocket size={14} /> Deploy Fix
                </button>
              </div>
            </div>
            
            <div className="terminal-wrapper">
              {incident.patch ? (
                <PatchTerminal filename={incident.patch.file} diffText={incident.patch.diff} />
              ) : (
                <div className="terminal-placeholder">
                  Executing autonomous repair heuristics...
                </div>
              )}
            </div>
          </div>

        </div>
      ) : (
        <div className="empty-state functional-empty">
          <ShieldCheck size={32} className="empty-icon-muted" />
          <div className="empty-text">No active incidents selected.</div>
        </div>
      )}
    </main>
  );
};
