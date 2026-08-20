// Next.js App Router dynamic route handler for creating a new DocumentMember

import { Document, DocumentMember } from '../types';

export async function POST(request: Request) {
  const params = new URLSearchParams(request.url.split('?')[1]);
  const documentId = params.get('documentId')!;
  const userId = (await request.json()).userId; // Assume the user ID is passed in the request body

  // Create a new DocumentMember with the specified role and user ID
  const result = await prisma.documentMember.create({
    data: {
      documentId,
      userId,
      role: 'editor', // Default to editor for simplicity
    },
  });

  if (!result) return new Response('Not Found', { status: 404 });

  // Return the created DocumentMember
  return new Response(JSON.stringify(result));
}