import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAegisStream } from '../hooks/useAegisStream';

// Mock WebSocket
class MockWebSocket {
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((msg: any) => void) | null = null;
  readyState = 1;

  constructor(url: string) {
    this.url = url;
  }
  send = vi.fn();
  close = vi.fn();
}

describe('useAegisStream Hook', () => {
  let originalWebSocket: any;

  beforeEach(() => {
    originalWebSocket = global.WebSocket;
    (global as any).WebSocket = MockWebSocket;
  });

  afterEach(() => {
    (global as any).WebSocket = originalWebSocket;
  });

  it('initializes with empty incidents and unconnected', () => {
    const { result } = renderHook(() => useAegisStream('ws://localhost:8000/ws'));
    
    expect(result.current.incidents).toEqual({});
    expect(result.current.connected).toBe(false);
  });
});
