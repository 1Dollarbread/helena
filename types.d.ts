// TypeScript interfaces and types for the Real-Time Collaborative Workspace project

export interface User {
  id: string;
  username: string;
  email: string;
}

export interface Document {
  id: string;
  title: string;
  content: string;
  members: DocumentMember[];
}

export interface DocumentMember {
  id: string;
  documentId: string;
  userId: string;
  role: 'owner' | 'editor' | 'viewer';
}