import { Program } from "@coral-xyz/anchor";
import { Connection, Keypair, sendAndConfirmTransaction, Transaction } from "@solana/web3.js";
import { program } from "..";
import idl from "../../target/idl/libertai_payment_processor.json";
import { LibertaiPaymentProcessor } from "../../target/types/libertai_payment_processor";
import { getKeypair } from "../utils";

const initialize = async (payer: Keypair, program: Program) => {
  const initializeIx = await program.methods
    .initialize(payer.publicKey)
    .accounts({
      payer: payer.publicKey,
    })
    .instruction();

  console.log("Crafting tx...");
  const tx = new Transaction().add(initializeIx);
  console.log("Sending tx...");
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log(`✅ Initialized. Tx Signature: ${sig}`);
};

export const InitializeCommand = async () => {
  const opts = program.opts();

  if (opts.payerKeyFilepath && opts.payerPrivateKey) {
    console.error("❌ Only one of --payer-key-filepath or --payer-private-key should be provided.");
    process.exit(1);
  }

  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });

  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const anchorProgram = new Program(idl as LibertaiPaymentProcessor, {
    connection,
  });

  await initialize(payer, anchorProgram);
}
