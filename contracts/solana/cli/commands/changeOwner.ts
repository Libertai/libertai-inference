import { Connection, Keypair, PublicKey, sendAndConfirmTransaction, Transaction } from "@solana/web3.js";
import { program } from "..";
import { getKeypair } from "../utils";
import { Program, BN } from "@coral-xyz/anchor";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import idl from "../../target/idl/libert_ai_payment_processor.json";

const changeOwner = async (
  payer: Keypair,
  newOwner: PublicKey,
  program: Program,
) => {
  const ix = await program.methods
    .changeOwner(newOwner)
    .accounts({
      payer: payer.publicKey,
    })
    .instruction();

  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log(`✅ Changed program owner to ${newOwner.toString()}. Tx Signature: ${sig}`);
}

export const ChangeOwnerCommand = async () => {
  const opts = program.opts();
  
  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });
  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const wallet = {
    publicKey: payer.publicKey,
    signTransaction: async (tx: Transaction) => {
      tx.partialSign(payer);
      return tx;
    },
    signAllTransactions: async (txs: Transaction[]) => {
      txs.forEach(tx => tx.partialSign(payer));
      return txs;
    },
  };
  const anchorProgram = new Program(idl as LibertAiPaymentProcessor, {
    connection,
    publicKey: wallet.publicKey,
  });

  const newOwner = new PublicKey(opts.newOwner)

  await changeOwner(
    payer,
    newOwner,
    anchorProgram
  );
}
