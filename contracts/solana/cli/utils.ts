import fs from "fs";
import { Keypair } from "@solana/web3.js";

const getPrivateKeyFromFile = (filepath: string): Keypair => {
  const secret = Uint8Array.from(JSON.parse(fs.readFileSync(filepath, "utf8")));
  return Keypair.fromSecretKey(secret);
};

export const getKeypair = (opts: { filepath?: string; key?: string }): Keypair => {
  if (opts.filepath) return getPrivateKeyFromFile(opts.filepath);
  if (opts.key) return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(opts.key)));
  throw new Error("Missing payer key input.");
};
