import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Sidebar } from '../components/Sidebar';
import type { Incident } from '../types';

// Mock jsdom limitation
window.HTMLElement.prototype.scrollIntoView = vi.fn();

describe('Sidebar Component', () => {
  const mockIncidents: Incident[] = [
    {
      id: 'inc-1',
      service: 'test-service',
      time: '12:00 PM',
      status: 'investigating',
      nodes_executed: []
    }
  ];

  it('renders correctly with incidents', () => {
    render(<Sidebar incidents={mockIncidents} connected={true} isOpen={true} onIncidentClick={vi.fn()} selectedIncidentId={null} />);
    expect(screen.getByText('test-service')).toBeDefined();
    expect(screen.getByText('LIVE')).toBeDefined();
  });

  it('calls onIncidentClick when an incident is clicked', () => {
    const handleClick = vi.fn();
    render(<Sidebar incidents={mockIncidents} connected={true} isOpen={true} onIncidentClick={handleClick} selectedIncidentId={null} />);
    
    const card = screen.getByText('test-service').closest('.event-card');
    fireEvent.click(card!);
    
    expect(handleClick).toHaveBeenCalledWith(mockIncidents[0]);
  });
});
