export interface ConversationalProfile {
    schema_version: 1;
    preferred_name: string | null;
    preferred_languages: string[];
    timezone_name: string | null;
    about_me: string | null;
}
export interface ApplicationProfileState {
    contract_version?: 'aurvek_applications.v1';
    version: number;
    profile: ConversationalProfile;
    contact: {phone_number: string | null; verified: boolean; version: string};
    channels: Array<{
        channel: 'phone' | 'whatsapp' | 'telegram';
        status: 'connected' | 'available' | 'connection_required' | 'configuration_pending';
        receivers: Array<{receiver_id: string; receiver_key: string; link_id: string | null; version: number | null; connected: boolean}>;
    }>;
}
