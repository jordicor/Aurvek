import { useEffect, useRef } from 'react';

export function AurvekProfilePanel({ bootstrap, appId, onReady, onMount, onEvent, uiLanguage,
    presentation = 'compact', title = 'Profile', className, style }) {
    const container = useRef(null), panel = useRef(null), callbacks = useRef({onReady, onMount, onEvent, uiLanguage});
    callbacks.current = {onReady, onMount, onEvent, uiLanguage};
    useEffect(() => {
        let active = true, instance;
        queueMicrotask(() => {
            if (!active) return;
            try {
                instance = window.AurvekApplications.mountProfile(container.current, {
                bootstrap, appId, presentation, title, onEvent: event => callbacks.current.onEvent?.(event),
            });
                panel.current = instance;
                callbacks.current.onMount?.(instance);
            } catch (error) {callbacks.current.onEvent?.({event: 'error', code: error.code || 'profile_mount_failed'}); return;}
            instance.ready.then(async () => {
                if (!active) return;
                if (callbacks.current.uiLanguage) await instance.setLanguage(callbacks.current.uiLanguage);
                if (active) callbacks.current.onReady?.(instance);
            })
                .catch(error => {if (active) callbacks.current.onEvent?.({event: 'error', code: error.code});});
        });
        return () => {active = false; panel.current = null; instance?.destroy();};
    }, [bootstrap, appId]);
    useEffect(() => {if (uiLanguage) void panel.current?.setLanguage(uiLanguage).catch(error => callbacks.current.onEvent?.({event: 'error', code: error.code}));}, [uiLanguage]);
    return <div ref={container} className={className} style={{height: 'min(840px, 90dvh)', ...style}} />;
}
