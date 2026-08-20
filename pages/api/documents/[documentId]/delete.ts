// Next.js App Router dynamic route handler for deleting a DocumentMember

import { Document, DocumentMember } from '../types';

export async function DELETE(request: Request) {
  const params = new URLSearchParams(request.url.split('?')[1]);
  const documentId = params.get('documentId')!;
  const userId = (await request.json()).userId; // Assume the user ID is passed in the request body

  // Delete the DocumentMember with the specified role and user ID
  const result = await prisma.documentMember.delete({
    where: {
      documentId_userId_role: {
        documentId,
        userId,
        role: 'editor', // Default to editor for simplicity, update as needed
      },
    },
  });

  if (!result) return new Response('Not Found', { status: 404 });

  // Return a success response
  return new Response(JSON.stringify(result));
}