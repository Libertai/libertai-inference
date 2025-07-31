import fs from "fs";
import { Connection, Keypair, PublicKey } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID } from "@solana/spl-token";

const getPrivateKeyFromFile = (filepath: string): Keypair => {
  const secret = Uint8Array.from(JSON.parse(fs.readFileSync(filepath, "utf8")));
  return Keypair.fromSecretKey(secret);
};

export const getKeypair = (opts: { filepath?: string; key?: string }): Keypair => {
  if (opts.filepath) return getPrivateKeyFromFile(opts.filepath);
  if (opts.key) return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(opts.key)));
  throw new Error("Missing payer key input.");
};

export const getTokenProgramId = async (
  connection: Connection,
  mint: PublicKey
): Promise<PublicKey> => {
  const mintInfo = await connection.getAccountInfo(mint);
  if (!mintInfo) {
    throw new Error(`Token mint ${mint.toString()} not found`);
  }
  
  return mintInfo.owner.equals(TOKEN_2022_PROGRAM_ID) 
    ? TOKEN_2022_PROGRAM_ID 
    : TOKEN_PROGRAM_ID;
};
