import { useState, useEffect, useCallback, useRef } from 'react';
import { supabase } from '../lib/supabase';
import type { SosIncidentStatus, SosRecentLocation } from '../types';

export function usePublicSosStatus(commandId: string | null) {
  const [status, setStatus] = useState<SosIncidentStatus | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [isExpiredOrNotFound, setIsExpiredOrNotFound] = useState<boolean>(false);
  const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

  // Auto-refresh interval ref
  const intervalRef = useRef<number | null>(null);

  const fetchStatus = useCallback(async (isInitial = false) => {
    if (!commandId) {
      setLoading(false);
      setIsExpiredOrNotFound(true);
      return;
    }

    if (isInitial) {
      setLoading(true);
    }

    try {
      // Call public RPC get_sos_incident_status(p_command_id) with anon key
      const { data, error: rpcError } = await supabase.rpc('get_sos_incident_status', {
        p_command_id: commandId,
      });

      if (rpcError) {
        console.error('Error fetching public SOS status:', rpcError);
        setError(rpcError.message);
        setIsExpiredOrNotFound(true);
        setLoading(false);
        return;
      }

      if (!data || !Array.isArray(data) || data.length === 0) {
        // Incident does not exist or was not type='sos'
        setStatus(null);
        setIsExpiredOrNotFound(true);
        setLoading(false);
        return;
      }

      const row = data[0];
      let recentLocations: SosRecentLocation[] = [];
      if (Array.isArray(row.recent_locations)) {
        recentLocations = row.recent_locations;
      } else if (typeof row.recent_locations === 'string') {
        try {
          recentLocations = JSON.parse(row.recent_locations);
        } catch {
          recentLocations = [];
        }
      }

      const incident: SosIncidentStatus = {
        command_id: row.command_id,
        command_status: row.command_status,
        command_created_at: row.command_created_at,
        plain_status: row.plain_status,
        reason: row.reason,
        mission_id: row.mission_id,
        recent_locations: recentLocations,
      };

      setStatus(incident);
      setError(null);
      setLastFetchedAt(new Date());

      if (row.plain_status === 'expired') {
        setIsExpiredOrNotFound(true);
      } else {
        setIsExpiredOrNotFound(false);
      }
    } catch (err: any) {
      console.error('Unexpected error fetching public SOS status:', err);
      setError(err?.message || 'Network error');
    } finally {
      setLoading(false);
    }
  }, [commandId]);

  useEffect(() => {
    fetchStatus(true);

    // Auto-refresh every 10 seconds
    intervalRef.current = window.setInterval(() => {
      fetchStatus(false);
    }, 10000);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [fetchStatus]);

  return {
    status,
    loading,
    error,
    isExpiredOrNotFound,
    lastFetchedAt,
    refetch: () => fetchStatus(false),
  };
}
