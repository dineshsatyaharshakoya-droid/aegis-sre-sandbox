import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MainView } from '../components/MainView';
import type { Incident } from '../types';

describe('MainView Component', () => {
  it('renders Empty State when no incident is selected', () => {
    render(
      <MainView 
        incident={null} 
        incidentCount={0}
        onApprove={vi.fn()} 
        onReject={vi.fn()} 
        toggleSidebar={vi.fn()} 
      />
    );
    expect(screen.getByText('No active incidents selected.')).toBeDefined();
  });

  it('renders Incident Timeline when incident is selected', () => {
    const mockIncident: Incident = {
      id: 'inc-1',
      service: 'critical-auth-service',
      crash_log: 'Traceback Error...',
      time: '12:00 PM',
      status: 'investigating',
      nodes_executed: ['planner', 'researcher']
    };

    render(
      <MainView 
        incident={mockIncident} 
        incidentCount={1}
        onApprove={vi.fn()} 
        onReject={vi.fn()} 
        toggleSidebar={vi.fn()} 
      />
    );
    
    // Check service title
    expect(screen.getByText('critical-auth-service')).toBeDefined();
    // Check crash log
    expect(screen.getByText('Traceback Error...')).toBeDefined();
    // Check that pipeline rendered the node
    expect(screen.getByText('planner')).toBeDefined();
  });
});
