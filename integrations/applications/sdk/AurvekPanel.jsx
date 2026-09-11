import { useEffect, useRef } from 'react';

// Load /static/js/aurvek-applications.js before mounting this component.
// Keep bootstrap stable for a frame's lifetime; issue a new ticket to reopen.
export function AurvekPanel({ bootstrap, appId, contextId, presentation = 'full', onReady, onEvent,
    title = 'Aurvek', className, style }) {
    const container = useRef(null);
    const panel = useRef(null);
    const callbacks = useRef({ onReady, onEvent });
    callbacks.current = { onReady, onEvent };
    useEffect(() => {
        let active = true;
        let instance;
        // React StrictMode replays effects. Do not consume the one-use ticket
        // in an effect that is immediately discarded.
        queueMicrotask(() => {
            if (!active) return;
            instance = window.AurvekApplications.mount(container.current, {
                bootstrap, appId, contextId, presentation, title,
                onEvent: event => callbacks.current.onEvent?.(event),
            });
            panel.current = instance;
            instance.ready.then(() => { if (active) callbacks.current.onReady?.(instance); })
                .catch(error => { if (active) callbacks.current.onEvent?.({ event: 'error', code: error.code }); });
        });
        return () => {
            active = false; panel.current = null;
            if (instance) void instance.close().catch(error => {
                callbacks.current.onEvent?.({ event: 'error', code: error.code });
                instance.destroy();
            });
        };
    }, [bootstrap, appId, contextId]);
    useEffect(() => { void panel.current?.setPresentation(presentation).catch(() => {}); }, [presentation]);
    return <div ref={container} className={className} style={{ height: 'min(760px, 90dvh)', ...style }} />;
}
