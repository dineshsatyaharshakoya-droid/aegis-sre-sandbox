import { useState } from 'react';
import { useAegisStream } from './hooks/useAegisStream';
import { Sidebar } from './components/Sidebar';
import { MainView } from './components/MainView';
import type { Incident } from './types';
import './styles/variables.css';
import './styles/layout.css';
import './styles/sidebar.css';
import './styles/mainview.css';
import './styles/components.css';
import './styles/terminal.css';
import './styles/responsive.css';

function App() {
  // Dynamic WebSocket URL: works on localhost, LAN, and public tunnels (ngrok)
  const wsUrl = import.meta.env.VITE_WS_URL || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
  const { incidents, connected, approvePatch, rejectPatch } = useAegisStream(wsUrl);
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [selectedIncident, setSelectedIncident] = useState<Incident | null>(null);

  const handleApprove = (file: string | undefined) => {
    approvePatch(file);
    setIsSidebarOpen(false); // Close drawer on mobile
  };

  return (
    <div className="dashboard-layout">
      {/* Mobile Overlay */}
      <div 
        className={`sidebar-overlay ${isSidebarOpen ? 'mobile-open' : ''}`} 
        onClick={() => setIsSidebarOpen(false)}
      />

      <Sidebar 
        incidents={Object.values(incidents)} 
        connected={connected} 
        isOpen={isSidebarOpen}
        onIncidentClick={(inc) => setSelectedIncident(inc)}
        selectedIncidentId={selectedIncident?.id || null}
      />

      <MainView 
        incident={selectedIncident}
        incidentCount={Object.keys(incidents).length}
        onApprove={handleApprove}
        onReject={() => {
          rejectPatch();
          setSelectedIncident(null);
        }}
        toggleSidebar={() => setIsSidebarOpen(true)}
      />
    </div>
  );
}

export default App;
