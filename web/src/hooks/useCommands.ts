import { useState, useEffect, useCallback } from 'react';
import { supabase } from '../lib/supabase';
import { APP_CONFIG } from '../lib/config';
import type { CommandRow, CommandType } from '../types';

export function useCommands(userEmail: string | null | undefined) {
  const [commands, setCommands] = useState<CommandRow[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [isSending, setIsSending] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<{ text: string; type: 'info' | 'success' | 'error' } | null>(null);

  const fetchCommands = useCallback(async () => {
    try {
      const { data, error } = await supabase
        .from('commands')
        .select('*')
        .eq('drone_id', APP_CONFIG.DRONE_ID)
        .order('issued_at', { ascending: false })
        .limit(20);

      if (!error && data) {
        setCommands(data as CommandRow[]);
      }
    } catch (err) {
      console.error('Error loading commands:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCommands();

    const channel = supabase
      .channel(`gss-commands-${APP_CONFIG.DRONE_ID}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'commands',
          filter: `drone_id=eq.${APP_CONFIG.DRONE_ID}`,
        },
        () => {
          fetchCommands();
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, [fetchCommands]);

  const issueCommand = async (
    type: CommandType,
    extraParams?: {
      target_lat?: number;
      target_lon?: number;
      target_alt_m?: number;
      params?: Record<string, any>;
      mission_id?: string;
    }
  ): Promise<{ success: boolean; error?: string }> => {
    setIsSending(true);
    setStatusMessage({ text: `Sending ${type.toUpperCase()} command...`, type: 'info' });

    try {
      const row: Partial<CommandRow> = {
        drone_id: APP_CONFIG.DRONE_ID,
        type,
        issued_by: userEmail || 'operator',
        status: 'pending',
        target_lat: extraParams?.target_lat ?? null,
        target_lon: extraParams?.target_lon ?? null,
        target_alt_m: extraParams?.target_alt_m ?? null,
        params: extraParams?.params ?? null,
        mission_id: extraParams?.mission_id ?? null,
      };

      const { error } = await supabase.from('commands').insert(row);

      if (error) {
        const errorMsg =
          type === 'sos'
            ? `SOS dispatch failed: ${error.message}`
            : `Command rejected: ${error.message}`;
        setStatusMessage({ text: errorMsg, type: 'error' });
        setIsSending(false);
        return { success: false, error: errorMsg };
      }

      const successMsg =
        type === 'sos'
          ? '✓ SOS sent — drone and WhatsApp alert triggered'
          : `✓ ${type.toUpperCase()} command queued! Awaiting GSS response...`;

      setStatusMessage({
        text: successMsg,
        type: 'success',
      });
      setIsSending(false);
      fetchCommands();
      return { success: true };
    } catch (err: any) {
      const msg = err.message || 'Unknown network error';
      const errorMsg =
        type === 'sos'
          ? `SOS dispatch failed: ${msg}`
          : `Failed to issue command: ${msg}`;
      setStatusMessage({ text: errorMsg, type: 'error' });
      setIsSending(false);
      return { success: false, error: errorMsg };
    }
  };

  return {
    commands,
    loading,
    isSending,
    statusMessage,
    clearStatusMessage: () => setStatusMessage(null),
    issueCommand,
    refetch: fetchCommands,
  };
}
