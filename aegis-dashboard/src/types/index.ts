export interface AegisEvent {
  id: string;
  incident_id?: string;
  time: string;
  type: 'telemetry_received' | 'node_update' | 'patch_ready' | 'patch_deployed' | 'error';
  service?: string;
  crash?: string;
  node?: string;
  state_summary?: string[];
  file?: string;
  root_cause_analysis?: string;
  explanation?: string;
  diff?: string;
  message?: string;
}

export interface Incident {
  id: string;
  service: string;
  crash_log?: string;
  time: string;
  status: 'investigating' | 'patch_ready' | 'deployed' | 'error';
  nodes_executed: string[];
  patch?: {
    file: string;
    diff: string;
    root_cause_analysis: string;
    explanation: string;
  };
}
